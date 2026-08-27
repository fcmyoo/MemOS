"""Route tests for the ``memory_layer`` filter on GET /me/memories.

Covers the P1 memory-layer feature landed on ``me_router.list_my_memories``:
L1/L2/L3/Skill equality filter pushed into ``export_graph`` with
``status=["activated"]``, the ``unclassified`` bucket via a ``not_in``
filter, 422 on an unknown layer value, and the plain (no ``memory_layer``)
path staying on ``TreeTextMemory.get_all`` without a status filter. Also
covers ``stats_available`` on /me/memories and ``layers_available`` on
/me/sync-status.

No real backend is started: ``memos.api.handlers.init_server`` is patched
with mock components (same set as ``tests/api/test_server_cube_access.py``)
before ``memos.api.routers.server_router`` is imported, then
``server_router.naive_mem_cube`` is swapped per-test for a fresh ``Mock()``
whose ``text_mem`` / ``text_mem.graph_store`` methods are stubbed directly.
Auth is bypassed via ``app.dependency_overrides`` on
``verify_web_access_token`` (session-bearer auth is exercised elsewhere, in
``tests/api/test_me_keys.py``); ``me_router._services`` is monkeypatched to
avoid needing a real ``UserManager`` / PostgreSQL-backed key store.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from memos.api.routers import me_router as me_router_module
from memos.mem_user.user_manager import UserRole


def _mock_components() -> dict:
    """Same mock component set used by test_server_cube_access.py etc."""
    return {
        "graph_db": Mock(),
        "mem_reader": Mock(),
        "llm": Mock(),
        "embedder": Mock(),
        "reranker": Mock(),
        "internet_retriever": Mock(),
        "memory_manager": Mock(),
        "default_cube_config": Mock(),
        "mos_server": Mock(),
        "mem_scheduler": Mock(),
        "feedback_server": Mock(),
        "naive_mem_cube": Mock(),
        "searcher": Mock(),
        "api_module": Mock(),
        "vector_db": None,
        "pref_extractor": None,
        "pref_adder": None,
        "pref_retriever": None,
        "pref_mem": None,
        "online_bot": None,
        "chat_llms": Mock(),
        "redis_client": Mock(),
        "deepsearch_agent": Mock(),
    }


@pytest.fixture(scope="module")
def server_router_module():
    """Import server_router once with init_server patched out.

    Module-scoped: the module-level component wiring only needs to happen
    once; each test then swaps ``naive_mem_cube`` for its own fresh mock.
    """
    with patch("memos.api.handlers.init_server", return_value=_mock_components()):
        from memos.api.routers import server_router

    return server_router


@pytest.fixture()
def naive_mem_cube(server_router_module, monkeypatch):
    """Fresh naive_mem_cube mock, wired onto server_router for this test."""
    naive = Mock()
    naive.text_mem.graph_store = Mock()
    monkeypatch.setattr(server_router_module, "naive_mem_cube", naive)
    return naive


@pytest.fixture()
def app(server_router_module, monkeypatch):
    """Bare FastAPI app with only me_router mounted, auth bypassed."""
    fastapi_app = FastAPI()
    fastapi_app.include_router(me_router_module.router)

    def _override_principal():
        return me_router_module.WebPrincipal(
            user_id="test-user", user_name="test-user", role=UserRole.USER
        )

    fastapi_app.dependency_overrides[me_router_module.verify_web_access_token] = (
        _override_principal
    )

    fake_services = SimpleNamespace(
        user_manager=SimpleNamespace(
            get_user=lambda user_id: SimpleNamespace(user_id=user_id, is_active=True),
            get_user_cubes=lambda user_id: [SimpleNamespace(cube_id="c1")],
        )
    )
    monkeypatch.setattr(me_router_module, "_services", lambda: fake_services)

    yield fastapi_app
    fastapi_app.dependency_overrides.clear()


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# GET /me/memories?memory_layer=...
# ---------------------------------------------------------------------------


def test_memory_layer_l2_filter(client, naive_mem_cube):
    """?memory_layer=L2 hits export_graph and every returned node is L2."""
    naive_mem_cube.text_mem.graph_store.export_graph.return_value = {
        "nodes": [
            {"id": "1", "memory": "m1", "metadata": {"memory_layer": "L2"}},
            {"id": "2", "memory": "m2", "metadata": {"memory_layer": "L2"}},
        ],
        "total_nodes": 71,
    }

    response = client.get("/me/memories", params={"memory_layer": "L2"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 71
    assert body["memories"]
    for node in body["memories"]:
        assert node["metadata"]["memory_layer"] == "L2"

    call_kwargs = naive_mem_cube.text_mem.graph_store.export_graph.call_args.kwargs
    assert call_kwargs["status"] == ["activated"]
    assert call_kwargs["filter"] == {"and": [{"memory_layer": "L2"}]}


def test_memory_layer_skill_empty(client, naive_mem_cube):
    """?memory_layer=Skill with no matching nodes returns total=0."""
    naive_mem_cube.text_mem.graph_store.export_graph.return_value = {
        "nodes": [],
        "total_nodes": 0,
    }

    response = client.get("/me/memories", params={"memory_layer": "Skill"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 0
    assert body["memories"] == []


def test_memory_layer_invalid(client, naive_mem_cube):
    """An unknown memory_layer value is rejected before touching the graph store."""
    response = client.get("/me/memories", params={"memory_layer": "XXX"})

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_memory_layer"
    naive_mem_cube.text_mem.graph_store.export_graph.assert_not_called()


def test_memory_layer_unclassified(client, naive_mem_cube):
    """?memory_layer=unclassified pushes a not_in filter and returns those nodes."""
    naive_mem_cube.text_mem.graph_store.export_graph.return_value = {
        "nodes": [{"id": "3", "memory": "m3", "metadata": {}}],
        "total_nodes": 1,
    }

    response = client.get("/me/memories", params={"memory_layer": "unclassified"})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["memories"][0]["id"] == "3"

    call_kwargs = naive_mem_cube.text_mem.graph_store.export_graph.call_args.kwargs
    assert call_kwargs["status"] == ["activated"]
    assert call_kwargs["filter"] == {
        "and": [{"memory_layer": {"not_in": ["L1", "L2", "L3", "Skill"]}}]
    }


def test_memory_layer_none_no_status_filter(client, naive_mem_cube):
    """No memory_layer param stays on get_all and never passes status=activated."""
    naive_mem_cube.text_mem.get_all.return_value = {
        "nodes": [{"id": "4", "memory": "m4", "metadata": {}}],
        "total_nodes": 1,
    }

    response = client.get("/me/memories")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1

    naive_mem_cube.text_mem.get_all.assert_called_once()
    call_kwargs = naive_mem_cube.text_mem.get_all.call_args.kwargs
    assert "status" not in call_kwargs
    naive_mem_cube.text_mem.graph_store.export_graph.assert_not_called()


def test_memories_stats_available(client, naive_mem_cube):
    """Type-distribution stats succeed and are reported as available."""
    naive_mem_cube.text_mem.get_all.return_value = {
        "nodes": [{"id": "5", "memory": "m5", "metadata": {}}],
        "total_nodes": 1,
    }
    naive_mem_cube.text_mem.graph_store.get_grouped_counts.return_value = [
        {"memory_type": "WorkingMemory", "count": 3},
        {"memory_type": "LongTermMemory", "count": 5},
    ]

    response = client.get("/me/memories")

    assert response.status_code == 200
    body = response.json()
    assert body["stats_available"] is True
    assert body["stats"]["by_type"] == {"WorkingMemory": 3, "LongTermMemory": 5}


# ---------------------------------------------------------------------------
# GET /me/sync-status
# ---------------------------------------------------------------------------


def test_sync_status_layers_available(client, naive_mem_cube, monkeypatch):
    """Per-layer grouped counts succeed, so layers_available stays True."""
    naive_mem_cube.text_mem.graph_store.get_grouped_counts.return_value = [
        {"memory_layer": "L2", "status": "activated", "count": 71},
        {"memory_layer": None, "status": "activated", "count": 4},
    ]
    # _query_layer_last_updated drives a raw Cypher session over
    # graph_store.driver; stub it out directly rather than building a fake
    # Neo4j driver/session pair just to exercise the aggregate query above.
    monkeypatch.setattr(me_router_module, "_query_layer_last_updated", lambda *_a, **_k: {})

    response = client.get("/me/sync-status")

    assert response.status_code == 200
    body = response.json()
    assert body["layers_available"] is True
    layer_counts = {row["layer"]: row["count"] for row in body["layers"]}
    assert layer_counts["L2"] == 71
    assert layer_counts["unclassified"] == 4
    assert body["total"] == 75
