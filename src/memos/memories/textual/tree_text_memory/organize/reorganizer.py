import json
import time
import traceback

from collections import defaultdict
from concurrent.futures import as_completed
from queue import PriorityQueue
from typing import Literal

import numpy as np

from memos.context.context import ContextThread, ContextThreadPoolExecutor
from memos.dependency import require_python_package
from memos.embedders.factory import OllamaEmbedder
from memos.graph_dbs.item import GraphDBEdge, GraphDBNode
from memos.graph_dbs.neo4j import Neo4jGraphDB
from memos.llms.base import BaseLLM
from memos.log import get_logger
from memos.memories.textual.item import SourceMessage, TreeNodeTextualMemoryMetadata
from memos.memories.textual.tree_text_memory.organize.handler import NodeHandler
from memos.memories.textual.tree_text_memory.organize.relation_reason_detector import (
    RelationAndReasoningDetector,
)
from memos.templates.tree_reorganize_prompts import (
    LOCAL_SUBCLUSTER_PROMPT,
    POLICY_INDUCTION_PROMPT,
    SKILL_INDUCTION_PROMPT,
    WORLD_MODEL_PROMPT,
)


logger = get_logger(__name__)


def build_summary_parent_node(cluster_nodes):
    normalized_sources = []
    for n in cluster_nodes:
        sm = SourceMessage(
            type="chat",
            role=None,
            chat_time=None,
            message_id=None,
            content=n.memory,
            # extra
            node_id=n.id,
        )
        normalized_sources.append(sm)
    return normalized_sources


class QueueMessage:
    def __init__(
        self,
        op: Literal["add", "remove", "merge", "update", "end"],
        # `str` for node and edge IDs, `GraphDBNode` and `GraphDBEdge` for actual objects
        before_node: list[str] | list[GraphDBNode] | None = None,
        before_edge: list[str] | list[GraphDBEdge] | None = None,
        after_node: list[str] | list[GraphDBNode] | None = None,
        after_edge: list[str] | list[GraphDBEdge] | None = None,
        user_name: str | None = None,
    ):
        self.op = op
        self.before_node = before_node
        self.before_edge = before_edge
        self.after_node = after_node
        self.after_edge = after_edge
        self.user_name = user_name

    def __str__(self) -> str:
        return f"QueueMessage(op={self.op}, before_node={self.before_node if self.before_node is None else len(self.before_node)}, after_node={self.after_node if self.after_node is None else len(self.after_node)})"

    def __lt__(self, other: "QueueMessage") -> bool:
        op_priority = {"add": 2, "remove": 2, "merge": 1, "end": 0}
        return op_priority[self.op] < op_priority[other.op]


def extract_first_to_last_brace(text: str):
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return "", None
    json_str = text[start : end + 1]
    return json_str, json.loads(json_str)


class GraphStructureReorganizer:
    # P1 L2 质化门槛（four-layer-gap-assessment.md 3.1，对齐 memmy policy-induction 语义）：
    # - SEMANTIC_THRESHOLD：簇内节点与锚点的余弦相似度门槛，剔除"时间相邻但语义无关"的节点
    # - MIN_POLICY_EVIDENCE：归纳 policy 所需的最少有效证据（节点）数，低于此不调用 LLM
    # - MIN_POLICY_GAIN：LLM 自评 gain_self_eval 门槛，低于此丢弃归纳结果（不生成 L2）
    SEMANTIC_THRESHOLD = 0.5
    MIN_POLICY_EVIDENCE = 3
    MIN_POLICY_GAIN = 0.3

    # P2 L3 world model 二级归纳门槛（four-layer-gap-assessment.md 3.2，对齐 memmy
    # world-model-pipeline 语义）：
    # - MIN_WORLD_EVIDENCE：归纳 world model 所需的最少同簇 L2 policy 数，低于此不调用 LLM
    # - MIN_WORLD_GAIN：LLM 自评 gain_self_eval 门槛，低于此丢弃归纳结果（不生成 L3）
    MIN_WORLD_EVIDENCE = 3
    MIN_WORLD_GAIN = 0.3

    # P3 Skill evidence/trial 机制门槛（four-layer-gap-assessment.md 3.3，对齐 memmy
    # skill-pipeline 语义）：
    # - MIN_SKILL_EVIDENCE：归纳 Skill 所需的最少正向 evidence 条数，低于此不调用 LLM
    # - MIN_SKILL_GAIN：LLM 自评 gain_self_eval 门槛，低于此丢弃归纳结果（不生成 Skill）
    MIN_SKILL_EVIDENCE = 2
    MIN_SKILL_GAIN = 0.3

    def __init__(
        self, graph_store: Neo4jGraphDB, llm: BaseLLM, embedder: OllamaEmbedder, is_reorganize: bool
    ):
        self.queue = PriorityQueue()  # Min-heap
        self.graph_store = graph_store
        self.llm = llm
        self.embedder = embedder
        self.relation_detector = RelationAndReasoningDetector(
            self.graph_store, self.llm, self.embedder
        )
        self.resolver = NodeHandler(graph_store=graph_store, llm=llm, embedder=embedder)

        self.is_reorganize = is_reorganize
        self._reorganize_needed = True
        # 状态无条件初始化：optimize_structure 依赖 _is_optimizing，
        # 与 is_reorganize 开关解耦（测试/手动调用路径也需要）
        self._stop_scheduler = False
        self._is_optimizing = {"LongTermMemory": False, "UserMemory": False}
        if self.is_reorganize:
            # ____ 1. For queue message driven thread ___________
            self.thread = ContextThread(target=self._run_message_consumer_loop)
            self.thread.start()
            # ____ 2. For periodic structure optimization _______
            self.structure_optimizer_thread = ContextThread(
                target=self._run_structure_organizer_loop
            )
            self.structure_optimizer_thread.start()

    def add_message(self, message: QueueMessage):
        self.queue.put_nowait(message)

    def _list_active_user_names(self) -> list[str]:
        """定时调度的用户范围：库中实际存在记忆的 user_name（即 user_id）。"""
        try:
            with self.graph_store.driver.session(database=self.graph_store.db_name) as session:
                rows = session.run(
                    "MATCH (n:Memory) WHERE n.status = 'activated' "
                    "RETURN DISTINCT n.user_name AS u LIMIT 50"
                ).data()
            return [r["u"] for r in rows if r.get("u")]
        except Exception:
            logger.warning("[GraphStructureReorganize] list active users failed", exc_info=True)
            return []

    def _optimize_all_users(self, scope: str, **kwargs) -> None:
        """对库中每个有记忆的用户各跑一轮结构优化。

        optimize_structure 的候选查询按 user_name 过滤（该属性存 user_id），
        调度路径不传用户会回落到 config 默认值（不在库中）→ 永远 0 候选。
        """
        users = self._list_active_user_names()
        # WARNING 级：生产日志仅 WARNING 可见（log.py:33），这是调度唯一可观察点
        logger.warning(
            "[Reorganizer] _optimize_all_users scope=%s users=%s optimizing=%s",
            scope, users, dict(self._is_optimizing),
        )
        for user_name in users:
            self.optimize_structure(scope=scope, user_name=user_name, **kwargs)
            # P2 L3：optimize_structure 产出新 L2 后立即消费归纳 world model。
            # _optimize_all_users 是 schedule 定时路径（每 100s）和 _reorganize_needed
            # 事件驱动路径（新节点触发）共同的调用入口，接在这里天然覆盖两条触发路径，
            # 无需在别处重复接线（four-layer-gap-assessment.md 3.2）。
            self.induce_world_models(user_name=user_name)
            # P3 Skill：induce_world_models 产出新 L3 后立即消费归纳 Skill（evidence 驱动）。
            # 接在这里天然覆盖定时与事件驱动两条触发路径（four-layer-gap-assessment.md 3.3）。
            self.induce_skills(user_name=user_name)

    def wait_until_current_task_done(self):
        """
        Wait until:
        1) queue is empty
        2) any running structure optimization is done
        """
        deadline = time.time() + 600
        if not self.is_reorganize:
            return

        if not self.queue.empty():
            self.queue.join()
        logger.debug("Queue is now empty.")

        while any(self._is_optimizing.values()):
            logger.debug(f"Waiting for structure optimizer to finish... {self._is_optimizing}")
            if time.time() > deadline:
                logger.error(f"Wait timed out; flags={self._is_optimizing}")
                break
            time.sleep(1)
        logger.debug("Structure optimizer is now idle.")

    def _run_message_consumer_loop(self):
        while True:
            message = self.queue.get()
            if message.op == "end":
                break

            try:
                if self._preprocess_message(message):
                    self.handle_message(message)
            except Exception:
                logger.error(traceback.format_exc())
            self.queue.task_done()

    @require_python_package(
        import_name="schedule",
        install_command="pip install schedule",
        install_link="https://schedule.readthedocs.io/en/stable/installation.html",
    )
    def _run_structure_organizer_loop(self):
        """
        Use schedule library to periodically trigger structure optimization.
        This runs until the stop flag is set.
        """
        import schedule

        schedule.every(100).seconds.do(self._optimize_all_users, scope="LongTermMemory")
        schedule.every(100).seconds.do(self._optimize_all_users, scope="UserMemory")

        # WARNING 级：生产环境日志级别为 WARNING（log.py:33），INFO 全被吞——
        # 调度线程存活与否只能靠这条日志观察，务必保持 WARNING 及以上。
        logger.warning("[Reorganizer] Structure optimizer schedule started.")

        while not getattr(self, "_stop_scheduler", False):
            schedule.run_pending()  # Drive schedule tasks (without this, registered tasks never execute)
            if any(self._is_optimizing.values()):
                time.sleep(1)
                continue
            if self._reorganize_needed:
                logger.warning("[Reorganizer] Triggering optimize_structure due to new nodes.")
                self._optimize_all_users(scope="LongTermMemory")
                self._optimize_all_users(scope="UserMemory")
                self._reorganize_needed = False
            time.sleep(30)

    def stop(self):
        """
        Stop the reorganizer thread.
        """
        if not self.is_reorganize:
            return

        self.add_message(QueueMessage(op="end"))
        self.thread.join()
        logger.info("Reorganize thread stopped.")
        self._stop_scheduler = True
        self.structure_optimizer_thread.join()
        logger.info("Structure optimizer stopped.")

    def handle_message(self, message: QueueMessage):
        handle_map = {"add": self.handle_add, "remove": self.handle_remove}
        handle_map[message.op](message)
        logger.debug(f"message queue size: {self.queue.qsize()}")

    def handle_add(self, message: QueueMessage):
        logger.debug(f"Handling add operation: {str(message)[:500]}")
        added_node = message.after_node[0]
        detected_relationships = self.resolver.detect(
            added_node,
            scope=added_node.metadata.memory_type,
            user_name=message.user_name,
        )
        if detected_relationships:
            for added_node, existing_node, relation in detected_relationships:
                self.resolver.resolve(
                    added_node, existing_node, relation, user_name=message.user_name
                )

        self._reorganize_needed = True

    def handle_remove(self, message: QueueMessage):
        logger.debug(f"Handling remove operation: {str(message)[:50]}")

    def optimize_structure(
        self,
        scope: str = "LongTermMemory",
        local_tree_threshold: int = 10,
        min_cluster_size: int = 4,
        min_group_size: int = 20,
        max_candidates: int = 40,
        max_duration_sec: int = 600,
        user_name: str | None = None,
    ):
        """
        Periodically reorganize the graph:
        1. Weakly partition nodes into clusters.
        2. Summarize each cluster.
        3. Create parent nodes and build local PARENT trees.
        """
        # --- Total time watch dog: check functions ---
        start_ts = time.time()

        def _check_deadline(where: str):
            if time.time() - start_ts > max_duration_sec:
                logger.error(
                    f"[GraphStructureReorganize] {scope} surpass {max_duration_sec}s，time "
                    f"over at {where}"
                )
                return True
            return False

        if self._is_optimizing[scope]:
            logger.info(f"[GraphStructureReorganize] Already optimizing for {scope}. Skipping.")
            return

        if self.graph_store.node_not_exist(scope, user_name=user_name):
            logger.debug(f"[GraphStructureReorganize] No nodes for scope={scope}. Skip.")
            return

        self._is_optimizing[scope] = True
        try:
            logger.debug(
                f"[GraphStructureReorganize] 🔍 Starting structure optimization for scope: {scope}"
            )

            logger.debug(
                f"[GraphStructureReorganize] Num of scope in self.graph_store is"
                f" {self.graph_store.get_memory_count(scope, user_name=user_name)}"
            )
            # Load candidate nodes
            if _check_deadline("[GraphStructureReorganize] Before loading candidates"):
                return
            raw_nodes = self.graph_store.get_structure_optimization_candidates(
                scope, user_name=user_name, max_candidates=max_candidates
            )
            # 分批消化：LIMIT 已下推 Neo4j（SQL 截断），Python 内存永不超载——
            # 此前 Python 侧全量加载 55713 候选（含 embedding）直接 OOM 杀 worker。
            nodes = [GraphDBNode(**n) for n in raw_nodes]

            # WARNING 级关键路径（生产 INFO 被吞，见 log.py:33）
            logger.warning(
                "[Reorganize] user=%s scope=%s candidates=%s -> using=%s",
                user_name, scope, len(raw_nodes), len(nodes),
            )
            if not nodes:
                logger.warning("[GraphStructureReorganize] No nodes to optimize. Skipping.")
                return
            if len(nodes) < min_group_size:
                logger.warning(
                    f"[GraphStructureReorganize] Only {len(nodes)} candidate nodes found. Not enough to reorganize. Skipping."
                )
                return

            # Step 2: Partition nodes
            if _check_deadline("[GraphStructureReorganize] Before partition"):
                return
            partitioned_groups = self._partition(nodes)
            logger.info(
                f"[GraphStructureReorganize] Partitioned into {len(partitioned_groups)} clusters."
            )

            if _check_deadline("[GraphStructureReorganize] Before submit partition task"):
                return
            with ContextThreadPoolExecutor(max_workers=4) as executor:
                futures = []
                for cluster_nodes in partitioned_groups:
                    futures.append(
                        executor.submit(
                            self._process_cluster_and_write,
                            cluster_nodes,
                            scope,
                            local_tree_threshold,
                            min_cluster_size,
                            user_name,
                        )
                    )

                for f in as_completed(futures):
                    if _check_deadline("[GraphStructureReorganize] Waiting clusters..."):
                        for x in futures:
                            x.cancel()
                        return
                    try:
                        f.result()
                    except Exception as e:
                        logger.warning(
                            f"[GraphStructureReorganize] Cluster processing failed: {e}, trace: {traceback.format_exc()}"
                        )
            logger.info("[GraphStructure Reorganize] Structure optimization finished.")

        finally:
            self._is_optimizing[scope] = False
            logger.info("[GraphStructureReorganize] Structure optimization finished.")

    def _process_cluster_and_write(
        self,
        cluster_nodes: list[GraphDBNode],
        scope: str,
        local_tree_threshold: int,
        min_cluster_size: int,
        user_name: str | None = None,
    ):
        if len(cluster_nodes) <= min_cluster_size:
            return

        # P1 L2 质化：语义相似度过滤 —— 时间相邻 ≠ 语义相近，先剔除与簇内锚点偏离的节点，
        # 避免"发消息+模型配置+脚本修改"这类混杂摘要（four-layer-gap-assessment.md 3.1）。
        # 仅用于 L2 归纳路径；下方 relation/reasoning 检测仍对原始 cluster_nodes 生效。
        semantic_nodes = self._semantic_filter(cluster_nodes)
        if len(semantic_nodes) < min_cluster_size:
            logger.warning(
                "[Reorganizer] semantic filter kept %s/%s nodes (< min_cluster_size=%s), "
                "skip L2 induction this round (nodes stay isolated to accumulate).",
                len(semantic_nodes), len(cluster_nodes), min_cluster_size,
            )
        else:
            # Large cluster ➜ local sub-clustering（对语义过滤后的节点做 policy 归纳）
            sub_clusters = self._local_subcluster(semantic_nodes)
            sub_parents = []

            for sub_nodes in sub_clusters:
                if len(sub_nodes) < min_cluster_size:
                    continue  # Skip tiny noise
                sub_parent_node = self._summarize_cluster(sub_nodes, scope)
                if sub_parent_node is None:
                    # gain 门槛未通过或 LLM 判定 no_policy：不生成 L2，节点保持孤立待积累
                    continue
                self._create_parent_node(sub_parent_node, user_name=user_name)
                self._link_cluster_nodes(sub_parent_node, sub_nodes, user_name=user_name)
                sub_parents.append(sub_parent_node)

            if sub_parents and len(sub_parents) >= min_cluster_size:
                cluster_parent_node = self._summarize_cluster(semantic_nodes, scope)
                if cluster_parent_node is not None:
                    self._create_parent_node(cluster_parent_node, user_name=user_name)
                    for sub_parent in sub_parents:
                        self.graph_store.add_edge(
                            cluster_parent_node.id, sub_parent.id, "PARENT", user_name=user_name
                        )

        logger.info("Adding relations/reasons")
        nodes_to_check = cluster_nodes
        exclude_ids = [n.id for n in nodes_to_check]

        with ContextThreadPoolExecutor(max_workers=4) as executor:
            futures = []
            for node in nodes_to_check:
                futures.append(
                    executor.submit(
                        self.relation_detector.process_node,
                        node,
                        exclude_ids,
                        10,  # top_k
                    )
                )

            for f in as_completed(futures, timeout=300):
                results = f.result()

                # 1) Add pairwise relations
                for rel in results["relations"]:
                    if not self.graph_store.edge_exists(
                        rel["source_id"],
                        rel["target_id"],
                        rel["relation_type"],
                        user_name=user_name,
                    ):
                        self.graph_store.add_edge(
                            rel["source_id"],
                            rel["target_id"],
                            rel["relation_type"],
                            user_name=user_name,
                        )

                # 2) Add inferred nodes and link to sources
                for inf_node in results["inferred_nodes"]:
                    self.graph_store.add_node(
                        inf_node.id,
                        inf_node.memory,
                        inf_node.metadata.model_dump(exclude_none=True),
                        user_name=user_name,
                    )
                    for src_id in inf_node.metadata.sources:
                        self.graph_store.add_edge(
                            src_id, inf_node.id, "INFERS", user_name=user_name
                        )

                # 3) Add sequence links
                for seq in results["sequence_links"]:
                    if not self.graph_store.edge_exists(
                        seq["from_id"], seq["to_id"], "FOLLOWS", user_name=user_name
                    ):
                        self.graph_store.add_edge(
                            seq["from_id"], seq["to_id"], "FOLLOWS", user_name=user_name
                        )

                # 4) Add aggregate concept nodes
                for agg_node in results["aggregate_nodes"]:
                    self.graph_store.add_node(
                        agg_node.id,
                        agg_node.memory,
                        agg_node.metadata.model_dump(exclude_none=True),
                        user_name=user_name,
                    )
                    for child_id in agg_node.metadata.sources:
                        self.graph_store.add_edge(
                            agg_node.id, child_id, "AGGREGATE_TO", user_name=user_name
                        )

        logger.info("[Reorganizer] Cluster relation/reasoning done.")

    def _local_subcluster(
        self, cluster_nodes: list[GraphDBNode], max_length: int = 15000
    ) -> list[list[GraphDBNode]]:
        """
        Use LLM to split a large cluster into semantically coherent sub-clusters.
        """
        if not cluster_nodes:
            return []

        # Prepare conversation-like input: ID + key + value
        scene_lines = []
        for node in cluster_nodes:
            line = f"- ID: {node.id} | Key: {node.metadata.key} | Value: {node.memory}"
            scene_lines.append(line)

        joined_scene = "\n".join(scene_lines)
        if len(joined_scene) > max_length:
            logger.warning("Sub-cluster too long")
        prompt = LOCAL_SUBCLUSTER_PROMPT.replace("{joined_scene}", joined_scene[:max_length])

        messages = [{"role": "user", "content": prompt}]
        response_text = self.llm.generate(messages)
        response_json = self._parse_json_result(response_text)
        assigned_ids = set()
        result_subclusters = []

        for cluster in response_json.get("clusters", []):
            ids = []
            for nid in cluster.get("ids", []):
                if nid not in assigned_ids:
                    ids.append(nid)
                    assigned_ids.add(nid)
            sub_nodes = [node for node in cluster_nodes if node.id in ids]
            if len(sub_nodes) >= 2:
                result_subclusters.append(sub_nodes)

        return result_subclusters

    @require_python_package(
        import_name="sklearn",
        install_command="pip install scikit-learn",
        install_link="https://scikit-learn.org/stable/install.html",
    )
    def _partition(self, nodes, min_cluster_size: int = 10, max_cluster_size: int = 20):
        """
        Partition nodes by:
        - If total nodes <= max_cluster_size -> return all nodes in one cluster.
        - If total nodes > max_cluster_size -> cluster by embeddings, recursively split.
        - Only keep clusters with size > min_cluster_size.

        Args:
            nodes: List of GraphDBNode
            min_cluster_size: Min size to keep a cluster as-is

        Returns:
            List of clusters, each as a list of GraphDBNode
        """
        from sklearn.cluster import MiniBatchKMeans

        if len(nodes) <= max_cluster_size:
            logger.info(
                f"[KMeansPartition] Node count {len(nodes)} <= {max_cluster_size}, skipping KMeans."
            )
            return [nodes]

        def recursive_clustering(nodes_list, depth=0):
            """Recursively split clusters until each is <= max_cluster_size."""
            indent = "  " * depth
            logger.info(
                f"{indent}[Recursive] Start clustering {len(nodes_list)} nodes at depth {depth}"
            )

            if len(nodes_list) <= max_cluster_size:
                logger.info(
                    f"{indent}[Recursive] Node count <= {max_cluster_size}, stop splitting."
                )
                return [nodes_list]
            # Try kmeans with k = ceil(len(nodes) / max_cluster_size)
            x_nodes = [n for n in nodes_list if n.metadata.embedding]
            x = np.array([n.metadata.embedding for n in x_nodes])

            if len(x) < min_cluster_size:
                logger.info(
                    f"{indent}[Recursive] Too few embeddings ({len(x)}), skipping clustering."
                )
                return [nodes_list]

            k = min(len(x), (len(nodes_list) + max_cluster_size - 1) // max_cluster_size)
            k = max(1, k)

            try:
                logger.info(f"{indent}[Recursive] Clustering with k={k} on {len(x)} points.")
                kmeans = MiniBatchKMeans(n_clusters=k, batch_size=256, random_state=42)
                labels = kmeans.fit_predict(x)

                label_groups = defaultdict(list)
                for node, label in zip(x_nodes, labels, strict=False):
                    label_groups[label].append(node)

                # Map: label -> nodes with no embedding (fallback group)
                no_embedding_nodes = [n for n in nodes_list if not n.metadata.embedding]
                if no_embedding_nodes:
                    logger.warning(
                        f"{indent}[Recursive] {len(no_embedding_nodes)} nodes have no embedding. Added to largest cluster."
                    )
                    # Assign to largest cluster
                    largest_label = max(label_groups.items(), key=lambda kv: len(kv[1]))[0]
                    label_groups[largest_label].extend(no_embedding_nodes)

                result = []
                for label, sub_group in label_groups.items():
                    logger.info(f"{indent}  Cluster-{label}: {len(sub_group)} nodes")
                    result.extend(recursive_clustering(sub_group, depth=depth + 1))
                return result

            except Exception as e:
                logger.warning(
                    f"{indent}[Recursive] Clustering failed: {e}, fallback to one cluster."
                )
                return [nodes_list]

        raw_clusters = recursive_clustering(nodes)
        filtered_clusters = [c for c in raw_clusters if len(c) > min_cluster_size]

        logger.info(f"[KMeansPartition] Total clusters before filtering: {len(raw_clusters)}")
        for i, cluster in enumerate(raw_clusters):
            logger.info(f"[KMeansPartition]   Cluster-{i}: {len(cluster)} nodes")

        logger.info(
            f"[KMeansPartition] Clusters after filtering (>{min_cluster_size}): {len(filtered_clusters)}"
        )

        return filtered_clusters

    def _semantic_filter(self, cluster_nodes: list[GraphDBNode]) -> list[GraphDBNode]:
        """按语义相似度过滤簇内节点（P1 L2 质化，four-layer-gap-assessment.md 3.1）。

        取簇内第一个具备 embedding 的节点为锚点，剔除与锚点余弦相似度 < SEMANTIC_THRESHOLD 的节点，
        解决"时间相邻但语义无关"混进同一摘要的问题（如"发消息+模型配置+脚本修改"）。
        无 embedding 的节点无法评估相似度，原样保留（存量数据兼容，不因缺失 embedding 被误伤）；
        簇内全员都没有 embedding（无锚点可选）时不过滤，返回原簇。
        """
        anchor = next((n for n in cluster_nodes if n.metadata.embedding), None)
        if anchor is None:
            return cluster_nodes

        anchor_vec = np.array(anchor.metadata.embedding, dtype=float)
        anchor_norm = np.linalg.norm(anchor_vec)
        if anchor_norm == 0:
            return cluster_nodes

        kept = []
        for n in cluster_nodes:
            if not n.metadata.embedding:
                kept.append(n)
                continue
            vec = np.array(n.metadata.embedding, dtype=float)
            vec_norm = np.linalg.norm(vec)
            sim = float(np.dot(anchor_vec, vec) / (anchor_norm * vec_norm)) if vec_norm else 0.0
            if sim >= self.SEMANTIC_THRESHOLD:
                kept.append(n)
        return kept

    def _summarize_cluster(
        self, cluster_nodes: list[GraphDBNode], scope: str
    ) -> GraphDBNode | None:
        """
        对同主题簇归纳 L2 policy（对齐 memmy policy-induction 语义，four-layer-gap-assessment.md 3.1）。

        质量门槛（未通过则返回 None，不生成 L2，节点保持孤立待积累）：
        - 有效节点数 < MIN_POLICY_EVIDENCE：证据太少，不调用 LLM
        - LLM 判定 no_policy=true：这批记忆无法归纳出可复用规则
        - gain_self_eval < MIN_POLICY_GAIN：规则可信度/收益不足
        """
        if not cluster_nodes:
            raise ValueError("Cluster nodes cannot be empty.")

        if len(cluster_nodes) < self.MIN_POLICY_EVIDENCE:
            logger.info(
                "[Reorganizer] cluster size %s < MIN_POLICY_EVIDENCE=%s, skip L2 induction "
                "(no LLM call).",
                len(cluster_nodes), self.MIN_POLICY_EVIDENCE,
            )
            return None

        memories_items_text = "\n\n".join(
            [
                f"{i}. key: {n.metadata.key}\nvalue: {n.memory}\nsummary:{n.metadata.background}"
                for i, n in enumerate(cluster_nodes)
            ]
        )

        # Build prompt
        prompt = POLICY_INDUCTION_PROMPT.replace("{memory_items_text}", memories_items_text)

        messages = [{"role": "user", "content": prompt}]
        response_text = self.llm.generate(messages)
        response_json = self._parse_json_result(response_text)

        if not response_json or response_json.get("no_policy"):
            logger.info(
                "[Reorganizer] LLM reported no_policy for cluster (size=%s): %s",
                len(cluster_nodes),
                response_json.get("reason", "") if response_json else "parse_failed",
            )
            return None

        try:
            gain_self_eval = float(response_json.get("gain_self_eval", 0.0))
        except (TypeError, ValueError):
            gain_self_eval = 0.0

        if gain_self_eval < self.MIN_POLICY_GAIN:
            logger.info(
                "[Reorganizer] gain_self_eval=%.3f < MIN_POLICY_GAIN=%s, skip L2 induction.",
                gain_self_eval, self.MIN_POLICY_GAIN,
            )
            return None

        # Extract fields（policy-induction schema：policy_key/policy_value，而非旧 key/value）
        parent_key = str(response_json.get("policy_key", "")).strip()
        parent_value = str(response_json.get("policy_value", "")).strip()
        parent_tags = response_json.get("tags", [])
        parent_background = str(response_json.get("summary", "")).strip()

        embedding = self.embedder.embed([parent_value])[0]

        parent_node = GraphDBNode(
            memory=parent_value,
            metadata=TreeNodeTextualMemoryMetadata(
                user_id=None,
                session_id=None,
                memory_type=scope,
                status="activated",
                key=parent_key,
                tags=parent_tags,
                embedding=embedding,
                usage=[],
                sources=build_summary_parent_node(cluster_nodes),
                background=parent_background,
                confidence=gain_self_eval,
                type="policy",
                # P1 L2 质化：type 由 "topic"（内容摘要）改为 "policy"（行为规则归纳），
                # confidence 由固定 0.66 改为 LLM 自评 gain_self_eval。
                # TextualMemoryMetadata extra="allow"，字段会持久化到 Neo4j 顶层属性，
                # 与 memmy 同步记忆的 memory_layer 口径一致，前端按层筛选即可命中。
                memory_layer="L2",
            ),
        )
        return parent_node

    def _fetch_unconsumed_l2_policies(
        self, user_name: str | None = None, limit: int = 40
    ) -> list[GraphDBNode]:
        """查询该用户尚未被 L3 归纳消费的 L2 policy（P2，four-layer-gap-assessment.md 3.2）。

        消费标记：world_induced 布尔属性挂在 L2 节点上，默认未设置（NULL）视为未消费。
        LIMIT 40：复用 P1 分批思路（optimize_structure 同款），防止一次性拉取过多节点 OOM。
        """
        where_clause = (
            "WHERE n.type = 'policy' AND n.memory_layer = 'L2' AND n.status = 'activated' "
            "AND (n.world_induced IS NULL OR n.world_induced = false)"
        )
        params: dict = {"limit": limit}
        if user_name:
            where_clause += " AND n.user_name = $user_name"
            params["user_name"] = user_name

        query = f"""
            MATCH (n:Memory)
            {where_clause}
            RETURN n.id AS id, n AS node
            ORDER BY n.created_at ASC
            LIMIT $limit
        """
        try:
            with self.graph_store.driver.session(database=self.graph_store.db_name) as session:
                rows = session.run(query, params).data()
            raw_nodes = [
                self.graph_store._parse_node({"id": r["id"], **dict(r["node"])}) for r in rows
            ]
            return [GraphDBNode(**n) for n in raw_nodes]
        except Exception:
            logger.warning(
                "[Reorganizer] fetch unconsumed L2 policies failed for user=%s", user_name,
                exc_info=True,
            )
            return []

    def induce_world_models(self, user_name: str | None = None) -> None:
        """P2 L3 二级归纳：消费该用户新产出且未被归纳过的 L2 policy，按语义聚类归纳
        跨场景稳定规律/用户画像（world model），对齐 memmy world-model-pipeline 语义
        （four-layer-gap-assessment.md 3.2）。

        L2 消费标记机制：world_induced 布尔属性默认未设置=未消费。无论簇内节点数是否
        达到 MIN_WORLD_EVIDENCE、LLM 是否判定 no_world_model、gain 是否达标，**本批拉取
        到的 L2（不只是产出 L3 的那部分）在处理完后统一标记 world_induced=true**——
        不可归纳的 L2 不应无限重试拖慢调度，保持孤立即可，新证据积累后会随后续新
        产出的 L2 一起在下一批被重新拉取聚类。
        """
        policy_nodes = self._fetch_unconsumed_l2_policies(user_name=user_name)
        if not policy_nodes:
            logger.warning(
                "[Reorganizer] induce_world_models user=%s: no unconsumed L2 policies.",
                user_name,
            )
            return

        # WARNING 级：生产日志仅 WARNING 可见（log.py:33），关键数字必须在此可观察
        logger.warning(
            "[Reorganizer] induce_world_models user=%s input_policies=%s",
            user_name, len(policy_nodes),
        )

        # 复用 _partition 做 embedding 语义聚类；max_cluster_size 调小以贴合 L3 场景
        # （L2 批量上限 40，需要按主题切成更细的簇，而非像 L1->L2 那样整批粗聚）。
        clusters = self._partition(policy_nodes, min_cluster_size=2, max_cluster_size=8)

        l3_count = 0
        skip_small_cluster = 0
        skip_no_world_or_low_gain = 0
        mark_failures: list[str] = []
        try:
            for cluster_nodes in clusters:
                if len(cluster_nodes) < self.MIN_WORLD_EVIDENCE:
                    skip_small_cluster += 1
                    logger.warning(
                        "[Reorganizer] world cluster size %s < MIN_WORLD_EVIDENCE=%s, "
                        "skip LLM call (no L3 induction).",
                        len(cluster_nodes), self.MIN_WORLD_EVIDENCE,
                    )
                    continue

                try:
                    world_node = self._summarize_world_model(cluster_nodes)
                    if world_node is None:
                        skip_no_world_or_low_gain += 1
                    else:
                        self._create_parent_node(world_node, user_name=user_name)
                        self._link_cluster_nodes(world_node, cluster_nodes, user_name=user_name)
                        l3_count += 1
                except Exception:
                    # 单簇异常隔离：LLM/embedding/写入失败只跳过本簇，不中止整批
                    logger.exception(
                        "[Reorganizer] world induction failed for cluster of %s nodes, skip.",
                        len(cluster_nodes),
                    )
        finally:
            # best-effort 消费标记：无论成功/no_world_model/低 gain/异常/未入簇，
            # 本批拉取到的所有 L2 最终都尝试置位，防无限重试（docstring 语义）
            for node in policy_nodes:
                try:
                    self.graph_store.update_node(node.id, {"world_induced": True}, user_name=user_name)
                except Exception:
                    mark_failures.append(node.id)
                    logger.exception("[Reorganizer] failed to mark world_induced for node %s", node.id)

        logger.warning(
            "[Reorganizer] induce_world_models done user=%s input_policies=%s output_l3=%s "
            "skip_small_cluster=%s skip_no_world_model_or_low_gain=%s mark_failures=%s",
            user_name, len(policy_nodes), l3_count, skip_small_cluster, skip_no_world_or_low_gain, mark_failures,
        )

    def _summarize_world_model(self, cluster_nodes: list[GraphDBNode]) -> GraphDBNode | None:
        """对同主题簇的多条 L2 policy 归纳 L3 world model（对齐 memmy world-model-pipeline
        语义，four-layer-gap-assessment.md 3.2）。

        质量门槛（未通过则返回 None，不生成 L3；调用方 induce_world_models 仍会把
        这批 L2 标记为已消费，不因归纳失败而无限重试）：
        - LLM 判定 no_world_model=true：这批 policy 彼此无共同主题，无法归纳出画像/规律
        - gain_self_eval < MIN_WORLD_GAIN：画像/规律的置信度/收益不足
        """
        if not cluster_nodes:
            raise ValueError("Cluster nodes cannot be empty.")

        memories_items_text = "\n\n".join(
            [
                f"{i}. policy_key: {n.metadata.key}\npolicy_value: {n.memory}\n"
                f"summary:{n.metadata.background}"
                for i, n in enumerate(cluster_nodes)
            ]
        )

        prompt = WORLD_MODEL_PROMPT.replace("{memory_items_text}", memories_items_text)

        messages = [{"role": "user", "content": prompt}]
        response_text = self.llm.generate(messages)
        response_json = self._parse_json_result(response_text)

        if not response_json or response_json.get("no_world_model"):
            logger.warning(
                "[Reorganizer] LLM reported no_world_model for cluster (size=%s): %s",
                len(cluster_nodes),
                response_json.get("reason", "") if response_json else "parse_failed",
            )
            return None

        try:
            gain_self_eval = float(response_json.get("gain_self_eval", 0.0))
        except (TypeError, ValueError):
            gain_self_eval = 0.0

        if gain_self_eval < self.MIN_WORLD_GAIN:
            logger.warning(
                "[Reorganizer] gain_self_eval=%.3f < MIN_WORLD_GAIN=%s, skip L3 induction.",
                gain_self_eval, self.MIN_WORLD_GAIN,
            )
            return None

        world_key = str(response_json.get("world_key", "")).strip()
        world_value = str(response_json.get("world_value", "")).strip()
        world_tags = response_json.get("tags", [])
        if not isinstance(world_tags, list):
            world_tags = []
        world_tags = [str(t) for t in world_tags]
        world_background = str(response_json.get("summary", "")).strip()

        if not world_key or not world_value:
            logger.warning(
                "[Reorganizer] world_key or world_value is empty after parsing, skip L3 induction.",
            )
            return None

        embedding = self.embedder.embed([world_value])[0]

        world_node = GraphDBNode(
            memory=world_value,
            metadata=TreeNodeTextualMemoryMetadata(
                user_id=None,
                session_id=None,
                memory_type=cluster_nodes[0].metadata.memory_type,
                status="activated",
                key=world_key,
                tags=world_tags,
                embedding=embedding,
                usage=[],
                # sources：来源 L2 policy 的 id 列表（复用 build_summary_parent_node，
                # 与 _summarize_cluster 的 L1->L2 来源记录方式一致）
                sources=build_summary_parent_node(cluster_nodes),
                background=world_background,
                confidence=gain_self_eval,
                type="world_model",
                memory_layer="L3",
            ),
        )
        return world_node

    def _fetch_evidence_qualified_l3(
        self, user_name: str | None = None, limit: int = 20
    ) -> list[GraphDBNode]:
        """查询该用户 evidence 达标且尚未被 Skill 归纳消费的 L3 world model
        （P3 Skill 机制，four-layer-gap-assessment.md 3.3）。

        消费标记：skill_induced 布尔属性挂在 L3 节点上，默认未设置（NULL）视为未消费。
        evidence 门槛：skill_evidence_count >= MIN_SKILL_EVIDENCE（默认 2），只有正向反馈
        达标的 L3 才会被拉取归纳 Skill（对齐 memmy skill-pipeline 的 minSupport 语义）。
        LIMIT 20：复用 P1/P2 分批思路，防止一次性拉取过多节点 OOM。
        """
        where_clause = (
            "WHERE n.type = 'world_model' AND n.memory_layer = 'L3' AND n.status = 'activated' "
            f"AND n.skill_evidence_count >= {self.MIN_SKILL_EVIDENCE} "
            "AND (n.skill_induced IS NULL OR n.skill_induced = false)"
        )
        params: dict = {"limit": limit}
        if user_name:
            where_clause += " AND n.user_name = $user_name"
            params["user_name"] = user_name

        query = f"""
            MATCH (n:Memory)
            {where_clause}
            RETURN n.id AS id, n AS node
            ORDER BY n.skill_evidence_count DESC, n.created_at ASC
            LIMIT $limit
        """
        try:
            with self.graph_store.driver.session(database=self.graph_store.db_name) as session:
                rows = session.run(query, params).data()
            raw_nodes = [
                self.graph_store._parse_node({"id": r["id"], **dict(r["node"])}) for r in rows
            ]
            return [GraphDBNode(**n) for n in raw_nodes]
        except Exception:
            logger.warning(
                "[Reorganizer] fetch evidence-qualified L3 failed for user=%s", user_name,
                exc_info=True,
            )
            return []

    def induce_skills(self, user_name: str | None = None) -> None:
        """P3 Skill 归纳：消费该用户 evidence 达标且未被归纳过的 L3 world model，
        按 evidence 验证提炼可执行技能（对齐 memmy skill-pipeline 的 evidence 驱动
        + trial 机制，four-layer-gap-assessment.md 3.3）。

        L3 消费标记机制：skill_induced 布尔属性默认未设置=未消费。无论 evidence 是否
        达标、LLM 是否判定 no_skill、gain 是否达标，**本批拉取到的 L3（不只是产出
        Skill 的那部分）在处理完后统一标记 skill_induced=true**——不可技能化的 L3
        不应无限重试拖慢调度，保持孤立即可，新 evidence 积累后会随后续新产出的 L3
        一起在下一批被重新拉取。

        Skill 草稿态（trial）：产出的 Skill 节点 skill_status="trial"（草稿），
        memmy 语义是"需实际成功使用 N 次后转 activated"——本阶段先落 trial 态，
        后续接实际执行反馈后转正（由 Hermes 工具执行结果反馈或用户确认驱动）。
        """
        l3_nodes = self._fetch_evidence_qualified_l3(user_name=user_name)
        if not l3_nodes:
            logger.warning(
                "[Reorganizer] induce_skills user=%s: no evidence-qualified L3 world models.",
                user_name,
            )
            return

        # WARNING 级：生产日志仅 WARNING 可见（log.py:33），关键数字必须在此可观察
        logger.warning(
            "[Reorganizer] induce_skills user=%s input_l3=%s",
            user_name, len(l3_nodes),
        )

        skill_count = 0
        skip_no_skill_or_low_gain = 0
        mark_failures: list[str] = []
        for l3_node in l3_nodes:
            try:
                skill_node = self._summarize_skill(l3_node)
                if skill_node is None:
                    skip_no_skill_or_low_gain += 1
                else:
                    self._create_parent_node(skill_node, user_name=user_name)
                    # Link: Skill → L3（sources 记录，PARENT 边表示层级关系）
                    self._link_cluster_nodes(skill_node, [l3_node], user_name=user_name)
                    skill_count += 1
            except Exception:
                logger.exception("[Reorganizer] skill induction failed for L3 %s, skip.", l3_node.id)
            finally:
                try:
                    self.graph_store.update_node(l3_node.id, {"skill_induced": True}, user_name=user_name)
                except Exception:
                    mark_failures.append(l3_node.id)
                    logger.exception("[Reorganizer] failed to mark skill_induced for node %s", l3_node.id)

        logger.warning(
            "[Reorganizer] induce_skills done user=%s input_l3=%s output_skill=%s "
            "skip_no_skill_or_low_gain=%s mark_failures=%s",
            user_name, len(l3_nodes), skill_count, skip_no_skill_or_low_gain, mark_failures,
        )

    def _summarize_skill(self, l3_node: GraphDBNode) -> GraphDBNode | None:
        """从 evidence 验证的 L3 world model 归纳可执行 Skill（对齐 memmy skill-pipeline
        语义，four-layer-gap-assessment.md 3.3）。

        质量门槛（未通过则返回 None，不生成 Skill；调用方 induce_skills 仍会把
        这个 L3 标记为已消费，不因归纳失败而无限重试）：
        - LLM 判定 no_skill=true：该 world model 无法转化为可执行技能（缺乏明确步骤）
        - gain_self_eval < MIN_SKILL_GAIN：技能的置信度/收益不足
        """
        if not l3_node:
            raise ValueError("L3 node cannot be empty.")

        meta = l3_node.metadata
        world_key = meta.key or ""
        world_value = l3_node.memory or ""
        world_summary = meta.background or ""

        # 读取 evidence 日志
        evidence_log_raw = getattr(meta, "skill_evidence_log", "[]")
        try:
            evidence_log = json.loads(evidence_log_raw) if isinstance(evidence_log_raw, str) else evidence_log_raw
        except (json.JSONDecodeError, TypeError):
            evidence_log = []

        evidence_items_text = "\n".join(
            [f"{i+1}. [{item.get('at', '')}] {item.get('note', '')}" for i, item in enumerate(evidence_log)]
        )

        # 构造 prompt（替换占位符）
        prompt = SKILL_INDUCTION_PROMPT.replace("{world_key}", world_key)
        prompt = prompt.replace("{world_value}", world_value)
        prompt = prompt.replace("{summary}", world_summary)
        prompt = prompt.replace("{evidence_items}", evidence_items_text)

        messages = [{"role": "user", "content": prompt}]
        response_text = self.llm.generate(messages)
        response_json = self._parse_json_result(response_text)

        if not response_json or response_json.get("no_skill"):
            logger.warning(
                "[Reorganizer] LLM reported no_skill for L3 (id=%s): %s",
                l3_node.id,
                response_json.get("reason", "") if response_json else "parse_failed",
            )
            return None

        try:
            gain_self_eval = float(response_json.get("gain_self_eval", 0.0))
        except (TypeError, ValueError):
            gain_self_eval = 0.0

        if gain_self_eval < self.MIN_SKILL_GAIN:
            logger.warning(
                "[Reorganizer] gain_self_eval=%.3f < MIN_SKILL_GAIN=%s, skip Skill induction.",
                gain_self_eval, self.MIN_SKILL_GAIN,
            )
            return None

        skill_key = str(response_json.get("skill_key", "")).strip()
        skill_value = str(response_json.get("skill_value", "")).strip()
        skill_trigger = str(response_json.get("trigger", "")).strip()
        skill_tags = response_json.get("tags", [])
        if not isinstance(skill_tags, list):
            skill_tags = []
        skill_tags = [str(t) for t in skill_tags]
        skill_summary = str(response_json.get("summary", "")).strip()
        raw_evidence_count = response_json.get("evidence_count", len(evidence_log))
        try:
            evidence_count = int(raw_evidence_count)
        except (TypeError, ValueError):
            evidence_count = len(evidence_log)

        embedding = self.embedder.embed([skill_value])[0]

        skill_node = GraphDBNode(
            memory=skill_value,
            metadata=TreeNodeTextualMemoryMetadata(
                user_id=None,
                session_id=None,
                memory_type=l3_node.metadata.memory_type,
                status="activated",
                key=skill_key,
                tags=skill_tags,
                embedding=embedding,
                usage=[],
                # sources：来源 L3 world model 的 id（单个来源，复用 build_summary_parent_node）
                sources=build_summary_parent_node([l3_node]),
                background=skill_summary,
                confidence=gain_self_eval,
                type="skill",
                memory_layer="Skill",
                # P3 Skill trial 机制（对齐 memmy skill-pipeline 的 candidateTrials 语义）：
                # 产出的 Skill 初始为 "trial" 草稿态，需实际成功使用 N 次后转 "activated"。
                # 本阶段先落 trial 态，后续接工具执行结果/用户确认反馈后转正。
                skill_status="trial",
                skill_trigger=skill_trigger,
                skill_evidence_count=evidence_count,
            ),
        )
        return skill_node

    def _parse_json_result(self, response_text):
        """解析 LLM JSON 输出；任何畸形输入（None/非字符串/非 JSON/非 dict）都返回 {}，绝不抛异常。"""
        if not isinstance(response_text, str):
            logger.warning("[Reorganizer] LLM response is not a string: %r, treat as parse failure.", type(response_text).__name__)
            return {}
        try:
            cleaned = response_text.replace("```", "").replace("json", "")
            result = extract_first_to_last_brace(cleaned)[1]
            if not isinstance(result, dict):
                logger.warning("[Reorganizer] LLM response parsed to non-dict: %s, treat as parse failure.", type(result).__name__)
                return {}
            return result
        except Exception as e:  # JSONDecodeError 及 extract 失败等一律兜底
            logger.warning("Failed to parse LLM response as JSON: %s\nRaw response:\n%s", e, response_text)
            return {}

    def _create_parent_node(self, parent_node: GraphDBNode, user_name: str | None = None) -> None:
        """
        Create a new parent node for the cluster.
        """
        self.graph_store.add_node(
            parent_node.id,
            parent_node.memory,
            parent_node.metadata.model_dump(exclude_none=True),
            user_name=user_name,
        )

    def _link_cluster_nodes(
        self,
        parent_node: GraphDBNode,
        child_nodes: list[GraphDBNode],
        user_name: str | None = None,
    ):
        """
        Add PARENT edges from the parent node to all nodes in the cluster.
        """
        for child in child_nodes:
            if not self.graph_store.edge_exists(
                parent_node.id, child.id, "PARENT", direction="OUTGOING", user_name=user_name
            ):
                self.graph_store.add_edge(parent_node.id, child.id, "PARENT", user_name=user_name)

    def _preprocess_message(self, message: QueueMessage) -> bool:
        message = self._convert_id_to_node(message)
        if message.after_node is None or None in message.after_node:
            logger.debug(
                f"Found non-existent node in after_node in message: {message}, skip this message."
            )
            return False
        return True

    def _convert_id_to_node(self, message: QueueMessage) -> QueueMessage:
        """
        Convert IDs in the message.after_node to GraphDBNode objects.
        """
        for i, node in enumerate(message.after_node or []):
            if not isinstance(node, str):
                continue
            raw_node = self.graph_store.get_node(
                node, include_embedding=True, user_name=message.user_name
            )
            if raw_node is None:
                logger.debug(f"Node with ID {node} not found in the graph store.")
                message.after_node[i] = None
            else:
                # 存量数据兼容：metadata.internal_info 可能被存成 JSON 字符串
                # （与 dream/contextualization._coerce_json_dict 同一问题），
                # GraphDBNode 校验要求 dict，先归一化避免消费循环死循环报错。
                metadata = raw_node.get("metadata")
                if isinstance(metadata, dict):
                    internal_info = metadata.get("internal_info")
                    if isinstance(internal_info, str) and internal_info.strip().startswith("{"):
                        try:
                            metadata["internal_info"] = json.loads(internal_info)
                        except json.JSONDecodeError:
                            metadata["internal_info"] = {}
                message.after_node[i] = GraphDBNode(**raw_node)
        return message
