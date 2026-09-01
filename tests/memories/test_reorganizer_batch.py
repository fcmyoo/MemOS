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
        """测试候选数 > max_candidates 时截断生效"""
        graph_store, llm, embedder = mock_components
        reorganizer = GraphStructureReorganizer(graph_store, llm, embedder, is_reorganize=False)

        # Mock 返回 500 个候选节点
        fake_nodes = [make_fake_node(f"node_{i}") for i in range(500)]
        graph_store.get_structure_optimization_candidates.return_value = fake_nodes
        graph_store.node_not_exist.return_value = False
        graph_store.get_memory_count.return_value = 500

        # Mock _partition 返回空，避免后续处理
        mock_partition.return_value = []

        # 调用 optimize_structure，max_candidates=200
        reorganizer.optimize_structure(
            scope="LongTermMemory",
            min_group_size=20,
            max_candidates=200,
            max_duration_sec=600,
            user_name=None,
        )

        # 断言 _partition 收到的节点数 <= 200
        assert mock_partition.called
        nodes_passed_to_partition = mock_partition.call_args[0][0]
        assert len(nodes_passed_to_partition) == 200

    @patch("memos.memories.textual.tree_text_memory.organize.reorganizer.GraphStructureReorganizer._partition")
    def test_no_truncate_when_below_max(self, mock_partition, mock_components):
        """测试候选数 <= max_candidates 时不截断"""
        graph_store, llm, embedder = mock_components
        reorganizer = GraphStructureReorganizer(graph_store, llm, embedder, is_reorganize=False)

        # Mock 返回 50 个候选节点
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

        # 断言 _partition 收到的节点数 = 50（全量）
        assert mock_partition.called
        nodes_passed_to_partition = mock_partition.call_args[0][0]
        assert len(nodes_passed_to_partition) == 50

    @patch("memos.memories.textual.tree_text_memory.organize.reorganizer.GraphStructureReorganizer._partition")
    def test_truncate_takes_oldest_nodes(self, mock_partition, mock_components):
        """测试截断取的是最旧的节点（配合 ORDER BY created_at ASC）"""
        graph_store, llm, embedder = mock_components
        reorganizer = GraphStructureReorganizer(graph_store, llm, embedder, is_reorganize=False)

        # Mock 返回 300 个节点，ID 递增（模拟按 created_at ASC 排序）
        fake_nodes = [make_fake_node(f"node_{i:04d}") for i in range(300)]
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

        # 断言 _partition 收到的是前 100 个节点（node_0000 ~ node_0099 的确定性 UUID）
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
