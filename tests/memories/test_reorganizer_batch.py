"""测试 reorganizer 的分批消化能力"""
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest

from memos.graph_dbs.item import GraphDBNode
from memos.memories.textual.item import TreeNodeTextualMemoryMetadata
from memos.memories.textual.tree_text_memory.organize.reorganizer import GraphStructureReorganizer


@pytest.fixture
def mock_components():
    """Mock graph_store, llm, embedder"""
    graph_store = MagicMock()
    llm = MagicMock()
    embedder = MagicMock()
    return graph_store, llm, embedder


def make_fake_node(node_id: str) -> dict:
    """构造假节点（dict 格式，模拟 graph_store 返回）。

    node_id 若非 UUID（如 node_0000），转成确定性 UUID——GraphDBNode 校验
    id 必须是合法 UUID，同时保留原始序号便于断言"取最旧"。
    """
    try:
        uuid.UUID(node_id)
    except ValueError:
        node_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"test://{node_id}"))
    return {
        "id": node_id,
        "memory": f"Memory content for {node_id}",
        "metadata": {
            "user_id": "test_user",
            "session_id": "test_session",
            "memory_type": "LongTermMemory",
            "status": "activated",
            "key": f"key_{node_id}",
            "tags": [],
            "embedding": [0.1] * 128,  # 假 embedding
            "usage": [],
            "sources": [],
            "background": "",
            "confidence": 0.5,
            "type": "event",
            "memory_layer": "L1",
        },
    }


class TestReorganizerBatch:
    """测试 optimize_structure 的分批截断逻辑"""

    def test_max_candidates_default_value(self, mock_components):
        """测试签名默认值 max_candidates=40（实测 40 候选可在看门狗内完成）"""
        graph_store, llm, embedder = mock_components
        reorganizer = GraphStructureReorganizer(graph_store, llm, embedder, is_reorganize=False)

        # 检查函数签名默认值（通过检查源码或调用行为）
        import inspect

        sig = inspect.signature(reorganizer.optimize_structure)
        assert sig.parameters["max_candidates"].default == 40

    @patch("memos.memories.textual.tree_text_memory.organize.reorganizer.GraphStructureReorganizer._partition")
    def test_truncate_candidates_when_exceeds_max(self, mock_partition, mock_components):
        """LIMIT 已下推 SQL 层：断言调用带 max_candidates 且 mock 返回已截断结果"""
        graph_store, llm, embedder = mock_components
        reorganizer = GraphStructureReorganizer(graph_store, llm, embedder, is_reorganize=False)

        # Mock 模拟 SQL 层已按 LIMIT 200 截断
        fake_nodes = [make_fake_node(f"node_{i}") for i in range(200)]
        graph_store.get_structure_optimization_candidates.return_value = fake_nodes
        graph_store.node_not_exist.return_value = False
        graph_store.get_memory_count.return_value = 500

        mock_partition.return_value = []

        reorganizer.optimize_structure(
            scope="LongTermMemory",
            min_group_size=20,
            max_candidates=200,
            max_duration_sec=600,
            user_name=None,
        )

        # 断言查询层收到 max_candidates 参数（SQL LIMIT）
        call_kwargs = graph_store.get_structure_optimization_candidates.call_args
        assert call_kwargs.kwargs.get("max_candidates") == 200
        assert mock_partition.called
        nodes_passed_to_partition = mock_partition.call_args[0][0]
        assert len(nodes_passed_to_partition) == 200

    @patch("memos.memories.textual.tree_text_memory.organize.reorganizer.GraphStructureReorganizer._partition")
    def test_no_truncate_when_below_max(self, mock_partition, mock_components):
        """测试候选数 <= max_candidates 时全量进入分区"""
        graph_store, llm, embedder = mock_components
        reorganizer = GraphStructureReorganizer(graph_store, llm, embedder, is_reorganize=False)

        fake_nodes = [make_fake_node(f"node_{i}") for i in range(50)]
        graph_store.get_structure_optimization_candidates.return_value = fake_nodes
        graph_store.node_not_exist.return_value = False
        graph_store.get_memory_count.return_value = 50

        mock_partition.return_value = []

        reorganizer.optimize_structure(
            scope="LongTermMemory",
            min_group_size=20,
            max_candidates=200,
            max_duration_sec=600,
            user_name=None,
        )

        assert mock_partition.called
        nodes_passed_to_partition = mock_partition.call_args[0][0]
        assert len(nodes_passed_to_partition) == 50

    @patch("memos.memories.textual.tree_text_memory.organize.reorganizer.GraphStructureReorganizer._partition")
    def test_truncate_takes_oldest_nodes(self, mock_partition, mock_components):
        """测试 LIMIT 下推：查询按 created_at ASC + max_candidates 参数"""
        graph_store, llm, embedder = mock_components
        reorganizer = GraphStructureReorganizer(graph_store, llm, embedder, is_reorganize=False)

        # Mock 返回 100 个节点（模拟 SQL 层已按 created_at ASC + LIMIT 100 截断）
        fake_nodes = [make_fake_node(f"node_{i:04d}") for i in range(100)]
        graph_store.get_structure_optimization_candidates.return_value = fake_nodes
        graph_store.node_not_exist.return_value = False
        graph_store.get_memory_count.return_value = 300

        mock_partition.return_value = []

        reorganizer.optimize_structure(
            scope="LongTermMemory",
            min_group_size=20,
            max_candidates=100,
            max_duration_sec=600,
            user_name=None,
        )

        # 断言查询层收到 LIMIT 参数，且 _partition 收到全部 100 个（顺序保持）
        call_kwargs = graph_store.get_structure_optimization_candidates.call_args
        assert call_kwargs.kwargs.get("max_candidates") == 100
        nodes_passed = mock_partition.call_args[0][0]
        assert len(nodes_passed) == 100
        expected_first = str(uuid.uuid5(uuid.NAMESPACE_URL, "test://node_0000"))
        expected_last = str(uuid.uuid5(uuid.NAMESPACE_URL, "test://node_0099"))
        assert nodes_passed[0].id == expected_first
        assert nodes_passed[-1].id == expected_last

    def test_min_group_size_filter_still_works(self, mock_components):
        """测试截断后，min_group_size 过滤仍生效"""
        graph_store, llm, embedder = mock_components
        reorganizer = GraphStructureReorganizer(graph_store, llm, embedder, is_reorganize=False)

        # Mock 返回 15 个候选节点（< min_group_size=20）
        fake_nodes = [make_fake_node(f"node_{i}") for i in range(15)]
        graph_store.get_structure_optimization_candidates.return_value = fake_nodes
        graph_store.node_not_exist.return_value = False
        graph_store.get_memory_count.return_value = 15

        # 调用 optimize_structure
        reorganizer.optimize_structure(
            scope="LongTermMemory",
            min_group_size=20,
            max_candidates=200,
            max_duration_sec=600,
            user_name=None,
        )

        # 断言 _partition 未被调用（因为候选数不足 min_group_size）
        # 我们通过日志或执行路径验证，这里简单验证 add_node 没被调用
        assert not graph_store.add_node.called


class TestL2PolicyInduction:
    """P1 L2 质化：_summarize_cluster 的 gain 门槛（four-layer-gap-assessment.md 3.1）"""

    def _make_nodes(self, n: int) -> list[GraphDBNode]:
        return [GraphDBNode(**make_fake_node(f"node_{i}")) for i in range(n)]

    def test_below_min_evidence_skips_llm_and_l2(self, mock_components):
        """聚类内有效节点数 < MIN_POLICY_EVIDENCE(3) 时，不调用 LLM，也不产生 L2 节点"""
        graph_store, llm, embedder = mock_components
        reorganizer = GraphStructureReorganizer(graph_store, llm, embedder, is_reorganize=False)

        cluster_nodes = self._make_nodes(2)
        result = reorganizer._summarize_cluster(cluster_nodes, "LongTermMemory")

        assert result is None
        assert not llm.generate.called

    def test_no_policy_response_skips_l2(self, mock_components):
        """LLM 判定 no_policy=true 时，不产生 L2 节点（即便节点数达标）"""
        graph_store, llm, embedder = mock_components
        reorganizer = GraphStructureReorganizer(graph_store, llm, embedder, is_reorganize=False)

        llm.generate.return_value = (
            '{"no_policy": true, "reason": "这些事件彼此无关，无法归纳出可复用规则"}'
        )

        cluster_nodes = self._make_nodes(3)
        result = reorganizer._summarize_cluster(cluster_nodes, "LongTermMemory")

        assert result is None
        assert llm.generate.called
        assert not embedder.embed.called


class TestWorldModelInduction:
    """测试 induce_world_models 的 P2 L3 二级归纳（four-layer-gap-assessment.md 3.2）"""

    def _make_policy_node(self, node_id: str) -> GraphDBNode:
        """构造 L2 policy 节点（memory_layer=L2, type=policy），供 world model 归纳消费"""
        data = make_fake_node(node_id)
        data["metadata"]["type"] = "policy"
        data["metadata"]["memory_layer"] = "L2"
        data["metadata"]["key"] = f"policy_key_{node_id}"
        return GraphDBNode(**data)

    def _make_policy_nodes(self, n: int) -> list[GraphDBNode]:
        return [self._make_policy_node(f"policy_{i}") for i in range(n)]

    def test_no_unconsumed_policies_skips_induction(self, mock_components):
        """无未消费 L2 policy 时，直接返回，不调用 LLM，也不标记消费"""
        graph_store, llm, embedder = mock_components
        reorganizer = GraphStructureReorganizer(graph_store, llm, embedder, is_reorganize=False)

        with patch.object(reorganizer, "_fetch_unconsumed_l2_policies", return_value=[]):
            reorganizer.induce_world_models(user_name="test_user")

        assert not llm.generate.called
        assert not graph_store.update_node.called

    def test_below_min_world_evidence_skips_llm_but_marks_consumed(self, mock_components):
        """簇内 L2 数 < MIN_WORLD_EVIDENCE(3) 时不调用 LLM，但该批 L2 仍标记 world_induced=True"""
        graph_store, llm, embedder = mock_components
        reorganizer = GraphStructureReorganizer(graph_store, llm, embedder, is_reorganize=False)

        policy_nodes = self._make_policy_nodes(2)

        with (
            patch.object(reorganizer, "_fetch_unconsumed_l2_policies", return_value=policy_nodes),
            patch.object(reorganizer, "_partition", return_value=[policy_nodes]),
        ):
            reorganizer.induce_world_models(user_name="test_user")

        assert not llm.generate.called
        assert graph_store.update_node.call_count == len(policy_nodes)
        updated_ids = {call.args[0] for call in graph_store.update_node.call_args_list}
        assert updated_ids == {n.id for n in policy_nodes}
        for call in graph_store.update_node.call_args_list:
            assert call.args[1] == {"world_induced": True}

    def test_no_world_model_response_skips_l3_but_marks_consumed(self, mock_components):
        """LLM 判定 no_world_model=true 时不产生 L3，但该批 L2 仍标记消费"""
        graph_store, llm, embedder = mock_components
        reorganizer = GraphStructureReorganizer(graph_store, llm, embedder, is_reorganize=False)

        policy_nodes = self._make_policy_nodes(3)
        llm.generate.return_value = (
            '{"no_world_model": true, "reason": "这些 policy 彼此无共同主题"}'
        )

        with (
            patch.object(reorganizer, "_fetch_unconsumed_l2_policies", return_value=policy_nodes),
            patch.object(reorganizer, "_partition", return_value=[policy_nodes]),
        ):
            reorganizer.induce_world_models(user_name="test_user")

        assert llm.generate.called
        assert not graph_store.add_node.called
        assert graph_store.update_node.call_count == len(policy_nodes)

    def test_low_gain_skips_l3_but_marks_consumed(self, mock_components):
        """gain_self_eval < MIN_WORLD_GAIN 时丢弃归纳结果，不产生 L3，但仍标记消费"""
        graph_store, llm, embedder = mock_components
        reorganizer = GraphStructureReorganizer(graph_store, llm, embedder, is_reorganize=False)

        policy_nodes = self._make_policy_nodes(3)
        llm.generate.return_value = (
            '{"world_key": "k", "world_value": "v", "summary": "s", "gain_self_eval": 0.1}'
        )

        with (
            patch.object(reorganizer, "_fetch_unconsumed_l2_policies", return_value=policy_nodes),
            patch.object(reorganizer, "_partition", return_value=[policy_nodes]),
        ):
            reorganizer.induce_world_models(user_name="test_user")

        assert llm.generate.called
        assert not graph_store.add_node.called
        assert graph_store.update_node.call_count == len(policy_nodes)

    def test_successful_induction_creates_l3_and_links(self, mock_components):
        """gain 达标且判定有 world model 时，创建 L3 父节点并挂 PARENT 边，同时标记消费"""
        graph_store, llm, embedder = mock_components
        reorganizer = GraphStructureReorganizer(graph_store, llm, embedder, is_reorganize=False)

        policy_nodes = self._make_policy_nodes(3)
        llm.generate.return_value = (
            '{"world_key": "prefers_terse_reviews", '
            '"world_value": "用户偏好简洁的代码评审反馈", '
            '"summary": "多次反馈都指向同一偏好", '
            '"tags": ["review"], "gain_self_eval": 0.8}'
        )
        embedder.embed.return_value = [[0.2] * 128]
        graph_store.edge_exists.return_value = False

        with (
            patch.object(reorganizer, "_fetch_unconsumed_l2_policies", return_value=policy_nodes),
            patch.object(reorganizer, "_partition", return_value=[policy_nodes]),
        ):
            reorganizer.induce_world_models(user_name="test_user")

        assert graph_store.add_node.called
        added_metadata = graph_store.add_node.call_args[0][2]
        assert added_metadata["memory_layer"] == "L3"
        assert added_metadata["type"] == "world_model"
        assert graph_store.add_edge.call_count == len(policy_nodes)
        assert graph_store.update_node.call_count == len(policy_nodes)

    def test_mixed_clusters_all_input_policies_marked_consumed(self, mock_components):
        """多簇混合结果（部分产出 L3、部分被跳过）时，本批拉取到的全部 L2 仍统一标记消费"""
        graph_store, llm, embedder = mock_components
        reorganizer = GraphStructureReorganizer(graph_store, llm, embedder, is_reorganize=False)

        cluster_ok = self._make_policy_nodes(3)
        cluster_small = [self._make_policy_node("policy_small_0")]
        all_policy_nodes = cluster_ok + cluster_small

        llm.generate.return_value = (
            '{"world_key": "k", "world_value": "v", "summary": "s", '
            '"tags": [], "gain_self_eval": 0.9}'
        )
        embedder.embed.return_value = [[0.3] * 128]
        graph_store.edge_exists.return_value = False

        with (
            patch.object(
                reorganizer, "_fetch_unconsumed_l2_policies", return_value=all_policy_nodes
            ),
            patch.object(reorganizer, "_partition", return_value=[cluster_ok, cluster_small]),
        ):
            reorganizer.induce_world_models(user_name="test_user")

        # cluster_small 只有 1 个节点 < MIN_WORLD_EVIDENCE，不产出 L3，
        # 但 update_node 仍覆盖全部输入的 4 个节点（不因归纳失败而无限重试）
        assert graph_store.add_node.call_count == 1
        assert graph_store.add_edge.call_count == len(cluster_ok)
        assert graph_store.update_node.call_count == len(all_policy_nodes)
        updated_ids = {call.args[0] for call in graph_store.update_node.call_args_list}
        assert updated_ids == {n.id for n in all_policy_nodes}
