"""
P0 cube access control, Tasks 3+4 (plan sections 3.3 / 5.3 rows #1-#12, 5.4).

Covers the twelve memory-entry endpoints:
search, add, create_cube, register_cube, get_all, get_memory,
get_memory_by_ids, delete_memory, delete/recover by record id, feedback,
suggestions.

Builds the same lightweight app fixture as ``test_auth_router_mounts.py`` and
overrides the exact ``verify_api_key`` object the entry module bound, so the
identity always comes from the (mocked) API key — never from headers or body.

The fixture seeds a throwaway SQLite database: ``alice`` owns ``alice-cube``,
``bob`` is associated with ``alice-cube`` (shared) and owns ``bob-cube``,
``mallory`` is an outsider owning ``mallory-cube``. The shared
``CubeAccessControl`` and the cube handler's ``UserManager`` are pointed at
that database for the duration of each test.
"""

import os
from unittest.mock import Mock, patch

import pytest

from fastapi.testclient import TestClient

from memos.api.access_control import FORBIDDEN_DETAIL
from memos.mem_user.user_manager import UserManager


FORBIDDEN_BODY = {"detail": FORBIDDEN_DETAIL}


def _mock_components() -> dict:
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


def regular_auth(user_name: str) -> dict:
    """Auth context of a regular API key (no master/internal/bypass flags)."""
    return {
        "user_name": user_name,
        "scopes": ["read", "write"],
        "is_master_key": False,
        "api_key_id": f"key-{user_name}",
    }


BYPASSED_AUTH = {
    "user_name": "default",
    "scopes": ["all"],
    "is_master_key": False,
    "auth_bypassed": True,
}


@pytest.fixture(scope="module")
def entry_module():
    """Import server_api with init_server patched out (idempotent if cached)."""
    with patch("memos.api.handlers.init_server", return_value=_mock_components()):
        from memos.api import server_api

        yield server_api


@pytest.fixture()
def seeded(entry_module, tmp_path, monkeypatch):
    """Seed users/cubes and point the shared access control at them."""
    from memos.api.routers import server_router

    manager = UserManager(db_path=str(tmp_path / "memos_users.db"))
    alice_id = manager.create_user("alice", user_id="alice-id")
    bob_id = manager.create_user("bob", user_id="bob-id")
    mallory_id = manager.create_user("mallory", user_id="mallory-id")
    manager.create_cube("alice-cube", owner_id=alice_id, cube_id="alice-cube")
    manager.create_cube("bob-cube", owner_id=bob_id, cube_id="bob-cube")
    manager.create_cube("mallory-cube", owner_id=mallory_id, cube_id="mallory-cube")
    assert manager.add_user_to_cube(bob_id, "alice-cube") is True

    monkeypatch.setattr(server_router.access_control, "user_manager", manager)
    monkeypatch.setattr(server_router.cube_handler, "user_manager", manager)

    return {
        "manager": manager,
        "alice_id": alice_id,
        "bob_id": bob_id,
        "mallory_id": mallory_id,
    }


@pytest.fixture()
def guards(entry_module, seeded, monkeypatch):
    """Replace business seams with recorders and reset component mocks.

    - cube-view builders on the three class-based handlers become recorders so
      "reached the business mock" and "never reached it" are both observable;
    - the shared naive_mem_cube / graph_db / llm mocks are reset and given
      deterministic defaults.
    """
    from memos.api.routers import server_router

    views = {}
    for name, handler_attr in (
        ("search", "search_handler"),
        ("add", "add_handler"),
        ("feedback", "feedback_handler"),
    ):
        handler = getattr(server_router, handler_attr)
        view = Mock(name=f"{name}_cube_view")
        record: list = []

        def fake_build(
            req,
            *args,
            actor_user_id=None,
            _handler=handler,
            _view=view,
            _record=record,
            **kwargs,
        ):
            _record.append((list(_handler._resolve_cube_ids(req, actor_user_id)), actor_user_id))
            return _view

        monkeypatch.setattr(handler, "_build_cube_view", fake_build)
        views[name] = {"view": view, "record": record}

    views["search"]["view"].search_memories.return_value = {"text_mem": [], "pref_mem": []}
    views["add"]["view"].add_memories.return_value = []
    views["add"]["view"].feedback_memories.return_value = []
    views["feedback"]["view"].feedback_memories.return_value = []

    naive = server_router.naive_mem_cube
    naive.reset_mock()
    naive.text_mem.get_by_ids.return_value = []
    naive.text_mem.get_all.return_value = {"nodes": [], "total_nodes": 0}
    naive.text_mem.search.return_value = []
    server_router.graph_db.reset_mock()
    server_router.llm.generate.return_value = '{"query": ["s1"]}'

    return views


@pytest.fixture()
def make_client(entry_module):
    """Client factory keyed on the entry module's bound verify_api_key."""

    def _client(auth: dict, headers: dict | None = None) -> TestClient:
        entry_module.app.dependency_overrides[entry_module.verify_api_key] = lambda: dict(auth)
        return TestClient(entry_module.app, headers=headers or {})

    yield _client
    entry_module.app.dependency_overrides.clear()


# =============================================================================
# Outsider: every endpoint returns the uniform 403 and skips business logic
# =============================================================================

UNAUTHORIZED_CASES = [
    (
        "search",
        {"query": "q", "user_id": "mallory-id", "readable_cube_ids": ["alice-cube"]},
    ),
    (
        "add",
        {
            "user_id": "mallory-id",
            "writable_cube_ids": ["alice-cube"],
            "messages": [{"role": "user", "content": "x"}],
        },
    ),
    ("create_cube", {"cube_name": "forged", "owner_id": "alice-id"}),
    (
        "register_cube",
        {
            "mem_cube_name_or_path": "alice-cube",
            "mem_cube_id": "alice-cube",
            "user_id": "mallory-id",
        },
    ),
    (
        "get_all",
        {"user_id": "mallory-id", "mem_cube_ids": ["alice-cube"], "memory_type": "text_mem"},
    ),
    ("get_memory", {"user_id": "mallory-id", "mem_cube_id": "alice-cube"}),
    ("get_memory_by_ids", ["alice-memory-id"]),
    (
        "delete_memory",
        {"writable_cube_ids": ["alice-cube"], "memory_ids": ["alice-memory-id"]},
    ),
    (
        "delete_memory_by_record_id",
        {"mem_cube_id": "alice-cube", "record_id": "record-1"},
    ),
    (
        "recover_memory_by_record_id",
        {"mem_cube_id": "alice-cube", "delete_record_id": "record-1"},
    ),
    (
        "feedback",
        {
            "user_id": "mallory-id",
            "writable_cube_ids": ["alice-cube"],
            "history": [],
            "feedback_content": "wrong",
        },
    ),
    ("suggestions", {"user_id": "mallory-id", "mem_cube_id": "alice-cube", "language": "en"}),
    (
        "scheduler_status",
        {"user_id": "alice-id"},
    ),
    (
        "scheduler_task_queue_status",
        {"user_id": "alice-id"},
    ),
    (
        "scheduler_wait",
        {"user_name": "alice-id"},
    ),
    (
        "scheduler_wait_stream",
        {"user_name": "alice-id"},
    ),
    (
        "chat_complete",
        {"user_id": "mallory-id", "query": "q", "readable_cube_ids": ["alice-cube"]},
    ),
    (
        "chat_stream",
        {"user_id": "mallory-id", "query": "q", "readable_cube_ids": ["alice-cube"]},
    ),
]

ENDPOINT_PATHS = {
    "search": "/product/search",
    "add": "/product/add",
    "create_cube": "/product/create_cube",
    "register_cube": "/product/register_cube",
    "get_all": "/product/get_all",
    "get_memory": "/product/get_memory",
    "get_memory_by_ids": "/product/get_memory_by_ids",
    "delete_memory": "/product/delete_memory",
    "delete_memory_by_record_id": "/product/delete_memory_by_record_id",
    "recover_memory_by_record_id": "/product/recover_memory_by_record_id",
    "feedback": "/product/feedback",
    "suggestions": "/product/suggestions",
    "scheduler_status": "/product/scheduler/status",
    "scheduler_task_queue_status": "/product/scheduler/task_queue_status",
    "scheduler_wait": "/product/scheduler/wait",
    "scheduler_wait_stream": "/product/scheduler/wait/stream",
    "chat_complete": "/product/chat/complete",
    "chat_stream": "/product/chat/stream",
}

GET_ENDPOINTS = {"scheduler_status", "scheduler_task_queue_status", "scheduler_wait_stream"}


@pytest.mark.parametrize(("endpoint", "json_body"), UNAUTHORIZED_CASES)
def test_unauthorized_product_endpoint_returns_uniform_403(
    entry_module, seeded, guards, make_client, endpoint, json_body
):
    from memos.api.routers import server_router

    client = make_client(regular_auth("mallory"))

    method = "get" if endpoint in GET_ENDPOINTS else "post"
    if method == "get":
        response = client.get(ENDPOINT_PATHS[endpoint], params=json_body)
    elif endpoint == "scheduler_wait":
        response = client.post(ENDPOINT_PATHS[endpoint], params=json_body)
    else:
        response = client.post(ENDPOINT_PATHS[endpoint], json=json_body)

    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY

    # The underlying business machinery must never have been touched.
    if endpoint == "search":
        assert guards["search"]["record"] == []
        guards["search"]["view"].search_memories.assert_not_called()
        server_router.naive_mem_cube.text_mem.search.assert_not_called()
    elif endpoint == "add":
        assert guards["add"]["record"] == []
        guards["add"]["view"].add_memories.assert_not_called()
    elif endpoint == "feedback":
        assert guards["feedback"]["record"] == []
        guards["feedback"]["view"].feedback_memories.assert_not_called()
    elif endpoint == "create_cube":
        # Side-effect coverage lives in test_unauthorized_create_cube_never_creates.
        pass
    elif endpoint == "get_all":
        server_router.naive_mem_cube.text_mem.get_all.assert_not_called()
        server_router.naive_mem_cube.text_mem.get_relevant_subgraph.assert_not_called()
    elif endpoint == "get_memory":
        server_router.naive_mem_cube.text_mem.get_all.assert_not_called()
    elif endpoint == "delete_memory":
        server_router.naive_mem_cube.text_mem.delete_by_memory_ids.assert_not_called()
        server_router.naive_mem_cube.text_mem.delete_by_filter.assert_not_called()
    elif endpoint == "delete_memory_by_record_id":
        server_router.graph_db.delete_node_by_mem_cube_id.assert_not_called()
    elif endpoint == "recover_memory_by_record_id":
        server_router.graph_db.recover_memory_by_mem_cube_id.assert_not_called()
    elif endpoint == "suggestions":
        server_router.naive_mem_cube.text_mem.search.assert_not_called()
        server_router.llm.generate.assert_not_called()


def test_unauthorized_create_cube_never_creates(entry_module, seeded, guards, make_client):
    client = make_client(regular_auth("mallory"))
    create_cube = Mock(side_effect=AssertionError("create_cube must not be called"))

    with patch.object(seeded["manager"], "create_cube", create_cube):
        response = client.post(
            "/product/create_cube", json={"cube_name": "forged", "owner_id": "alice-id"}
        )

    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
    create_cube.assert_not_called()


def test_missing_cube_indistinguishable_from_unauthorized(
    entry_module, seeded, guards, make_client
):
    client = make_client(regular_auth("mallory"))

    missing = client.post(
        "/product/search",
        json={"query": "q", "user_id": "mallory-id", "readable_cube_ids": ["no-such-cube"]},
    )
    unauthorized = client.post(
        "/product/search",
        json={"query": "q", "user_id": "mallory-id", "readable_cube_ids": ["alice-cube"]},
    )

    assert missing.status_code == unauthorized.status_code == 403
    assert missing.json() == unauthorized.json() == FORBIDDEN_BODY


def test_multi_cube_list_with_one_unauthorized_denies_whole_request(
    entry_module, seeded, guards, make_client
):
    client = make_client(regular_auth("bob"))

    response = client.post(
        "/product/search",
        json={
            "query": "q",
            "user_id": seeded["bob_id"],
            "readable_cube_ids": ["alice-cube", "mallory-cube"],
        },
    )

    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
    assert guards["search"]["record"] == []


def test_header_spoof_is_ignored_key_identity_wins(entry_module, seeded, guards, make_client):
    # X-User-Name: alice must not upgrade mallory's key.
    client = make_client(regular_auth("mallory"), headers={"X-User-Name": "alice"})

    response = client.post(
        "/product/search",
        json={"query": "q", "user_id": "alice-id", "readable_cube_ids": ["alice-cube"]},
    )

    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY


def test_body_claimed_user_id_of_another_user_is_rejected(
    entry_module, seeded, guards, make_client
):
    client = make_client(regular_auth("mallory"))

    response = client.post(
        "/product/add",
        json={
            "user_id": "alice-id",
            "writable_cube_ids": ["alice-cube"],
            "messages": [{"role": "user", "content": "x"}],
        },
    )

    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
    assert guards["add"]["record"] == []


# =============================================================================
# Owner / shared access reaches the business mocks
# =============================================================================


def test_search_owner_reaches_business_mock(entry_module, seeded, guards, make_client):
    client = make_client(regular_auth("alice"))

    response = client.post(
        "/product/search",
        json={"query": "q", "user_id": seeded["alice_id"], "readable_cube_ids": ["alice-cube"]},
    )

    assert response.status_code == 200
    guards["search"]["view"].search_memories.assert_called_once()
    assert guards["search"]["record"] == [(["alice-cube"], seeded["alice_id"])]


def test_search_shared_user_reaches_business_mock(entry_module, seeded, guards, make_client):
    client = make_client(regular_auth("bob"))

    response = client.post(
        "/product/search",
        json={"query": "q", "user_id": seeded["bob_id"], "readable_cube_ids": ["alice-cube"]},
    )

    assert response.status_code == 200
    guards["search"]["view"].search_memories.assert_called_once()


def test_search_empty_cube_list_falls_back_to_actor_cube(
    entry_module, seeded, guards, make_client
):
    client = make_client(regular_auth("alice"))

    response = client.post(
        "/product/search", json={"query": "q", "user_id": seeded["alice_id"]}
    )

    assert response.status_code == 200
    # No readable_cube_ids -> actor's own cube id, never the raw claimed user.
    assert guards["search"]["record"] == [(["alice-id"], "alice-id")]


def test_add_owner_reaches_business_mock(entry_module, seeded, guards, make_client):
    client = make_client(regular_auth("alice"))

    response = client.post(
        "/product/add",
        json={
            "user_id": seeded["alice_id"],
            "writable_cube_ids": ["alice-cube"],
            "messages": [{"role": "user", "content": "remember this"}],
        },
    )

    assert response.status_code == 200
    guards["add"]["view"].add_memories.assert_called_once()
    assert guards["add"]["record"] == [(["alice-cube"], seeded["alice_id"])]


def test_add_shared_user_and_actor_fallback(entry_module, seeded, guards, make_client):
    client = make_client(regular_auth("bob"))

    shared = client.post(
        "/product/add",
        json={
            "user_id": seeded["bob_id"],
            "writable_cube_ids": ["alice-cube"],
            "messages": [{"role": "user", "content": "x"}],
        },
    )
    assert shared.status_code == 200
    assert guards["add"]["record"][-1] == (["alice-cube"], seeded["bob_id"])

    fallback = client.post(
        "/product/add",
        json={"user_id": seeded["bob_id"], "messages": [{"role": "user", "content": "y"}]},
    )
    assert fallback.status_code == 200
    assert guards["add"]["record"][-1] == (["bob-id"], seeded["bob_id"])


def test_create_cube_for_self_succeeds(entry_module, seeded, guards, make_client):
    client = make_client(regular_auth("alice"))

    response = client.post(
        "/product/create_cube", json={"cube_name": "my-cube", "owner_id": seeded["alice_id"]}
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["owner_id"] == seeded["alice_id"]
    assert data["cube_id"]


def test_register_cube_shared_user_succeeds(entry_module, seeded, guards, make_client):
    client = make_client(regular_auth("bob"))

    response = client.post(
        "/product/register_cube",
        json={
            "mem_cube_name_or_path": "alice-cube",
            "mem_cube_id": "alice-cube",
            "user_id": seeded["bob_id"],
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["cube_id"] == "alice-cube"


def test_feedback_owner_reaches_business_mock(entry_module, seeded, guards, make_client):
    client = make_client(regular_auth("alice"))

    response = client.post(
        "/product/feedback",
        json={
            "user_id": seeded["alice_id"],
            "writable_cube_ids": ["alice-cube"],
            "history": [],
            "feedback_content": "that was wrong",
        },
    )

    assert response.status_code == 200
    guards["feedback"]["view"].feedback_memories.assert_called_once()
    assert guards["feedback"]["record"] == [(["alice-cube"], seeded["alice_id"])]


def test_suggestions_owner_reaches_business_mock(entry_module, seeded, guards, make_client):
    from memos.api.routers import server_router

    client = make_client(regular_auth("alice"))

    response = client.post(
        "/product/suggestions",
        json={"user_id": seeded["alice_id"], "mem_cube_id": "alice-cube", "language": "en"},
    )

    assert response.status_code == 200
    # The cube (not the claimed user id) scopes the memory search.
    server_router.naive_mem_cube.text_mem.search.assert_called_once()
    assert (
        server_router.naive_mem_cube.text_mem.search.call_args.kwargs["user_name"]
        == "alice-cube"
    )


# =============================================================================
# Memory reads/deletes by id (5.4 all-or-nothing rules)
# =============================================================================


def test_get_all_owner_reaches_handler(entry_module, seeded, guards, make_client):
    from memos.api.routers import server_router

    server_router.naive_mem_cube.text_mem.get_all.return_value = {"nodes": [], "edges": []}
    client = make_client(regular_auth("alice"))

    response = client.post(
        "/product/get_all",
        json={
            "user_id": seeded["alice_id"],
            "mem_cube_ids": ["alice-cube"],
            "memory_type": "text_mem",
        },
    )

    assert response.status_code == 200
    server_router.naive_mem_cube.text_mem.get_all.assert_called_once()
    assert (
        server_router.naive_mem_cube.text_mem.get_all.call_args.kwargs["user_name"]
        == "alice-cube"
    )


def test_get_memory_owner_reaches_handler(entry_module, seeded, guards, make_client):
    from memos.api.routers import server_router

    client = make_client(regular_auth("alice"))

    response = client.post(
        "/product/get_memory",
        json={"user_id": seeded["alice_id"], "mem_cube_id": "alice-cube"},
    )

    assert response.status_code == 200
    server_router.naive_mem_cube.text_mem.get_all.assert_called()
    assert (
        server_router.naive_mem_cube.text_mem.get_all.call_args.kwargs["user_name"]
        == "alice-cube"
    )


def _memory_doc(memory_id: str, cube_id: str | None) -> dict:
    metadata: dict = {"id": memory_id}
    if cube_id is not None:
        metadata["user_name"] = cube_id
    return {"id": memory_id, "memory": "text", "metadata": metadata}


def test_get_memory_by_ids_authorized_metadata_passes(entry_module, seeded, guards, make_client):
    from memos.api.routers import server_router

    server_router.naive_mem_cube.text_mem.get_by_ids.return_value = [
        _memory_doc("m1", "alice-cube")
    ]
    client = make_client(regular_auth("alice"))

    response = client.post("/product/get_memory_by_ids", json=["m1"])

    assert response.status_code == 200
    assert len(response.json()["data"]["memories"]) == 1


def test_get_memory_by_ids_mixed_cube_denied(entry_module, seeded, guards, make_client):
    from memos.api.routers import server_router

    server_router.naive_mem_cube.text_mem.get_by_ids.return_value = [
        _memory_doc("m1", "alice-cube"),
        _memory_doc("m2", "mallory-cube"),
    ]
    client = make_client(regular_auth("bob"))

    response = client.post("/product/get_memory_by_ids", json=["m1", "m2"])

    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY


@pytest.mark.parametrize(
    ("returned", "requested"),
    [
        ([_memory_doc("m1", None)], ["m1"]),  # missing cube metadata
        ([_memory_doc("m1", "alice-cube")], ["m1", "m2"]),  # missing id
    ],
)
def test_get_memory_by_ids_missing_metadata_or_id_denied(
    entry_module, seeded, guards, make_client, returned, requested
):
    from memos.api.routers import server_router

    server_router.naive_mem_cube.text_mem.get_by_ids.return_value = returned
    client = make_client(regular_auth("alice"))

    response = client.post("/product/get_memory_by_ids", json=requested)

    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY


def test_delete_by_memory_ids_owner_ok_side_effect_happens(
    entry_module, seeded, guards, make_client
):
    from memos.api.routers import server_router

    server_router.naive_mem_cube.text_mem.get_by_ids.return_value = [
        _memory_doc("m1", "alice-cube")
    ]
    client = make_client(regular_auth("alice"))

    response = client.post("/product/delete_memory", json={"memory_ids": ["m1"]})

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "success"
    server_router.naive_mem_cube.text_mem.delete_by_memory_ids.assert_called_once_with(["m1"])


def test_delete_with_writable_cubes_owner_ok(entry_module, seeded, guards, make_client):
    from memos.api.routers import server_router

    server_router.naive_mem_cube.text_mem.get_by_ids.return_value = [
        _memory_doc("m1", "alice-cube")
    ]
    client = make_client(regular_auth("alice"))

    response = client.post(
        "/product/delete_memory",
        json={"writable_cube_ids": ["alice-cube"], "memory_ids": ["m1"]},
    )

    assert response.status_code == 200
    server_router.naive_mem_cube.text_mem.delete_by_memory_ids.assert_called_once_with(["m1"])


def test_delete_by_memory_ids_mixed_cube_denied_no_side_effect(
    entry_module, seeded, guards, make_client
):
    from memos.api.routers import server_router

    server_router.naive_mem_cube.text_mem.get_by_ids.return_value = [
        _memory_doc("m1", "alice-cube"),
        _memory_doc("m2", "mallory-cube"),
    ]
    client = make_client(regular_auth("bob"))

    response = client.post("/product/delete_memory", json={"memory_ids": ["m1", "m2"]})

    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
    server_router.naive_mem_cube.text_mem.delete_by_memory_ids.assert_not_called()


def test_delete_by_memory_ids_missing_metadata_denied_no_side_effect(
    entry_module, seeded, guards, make_client
):
    from memos.api.routers import server_router

    server_router.naive_mem_cube.text_mem.get_by_ids.return_value = [_memory_doc("m1", None)]
    client = make_client(regular_auth("alice"))

    response = client.post("/product/delete_memory", json={"memory_ids": ["m1"]})

    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
    server_router.naive_mem_cube.text_mem.delete_by_memory_ids.assert_not_called()


def test_delete_by_record_id_owner_ok(entry_module, seeded, guards, make_client):
    from memos.api.routers import server_router

    client = make_client(regular_auth("alice"))

    response = client.post(
        "/product/delete_memory_by_record_id",
        json={"mem_cube_id": "alice-cube", "record_id": "record-1"},
    )

    assert response.status_code == 200
    server_router.graph_db.delete_node_by_mem_cube_id.assert_called_once_with(
        mem_cube_id="alice-cube", delete_record_id="record-1", hard_delete=False
    )


def test_recover_by_record_id_owner_ok(entry_module, seeded, guards, make_client):
    from memos.api.routers import server_router

    client = make_client(regular_auth("alice"))

    response = client.post(
        "/product/recover_memory_by_record_id",
        json={"mem_cube_id": "alice-cube", "delete_record_id": "record-1"},
    )

    assert response.status_code == 200
    server_router.graph_db.recover_memory_by_mem_cube_id.assert_called_once_with(
        mem_cube_id="alice-cube", delete_record_id="record-1"
    )


# =============================================================================
# AUTH_ENABLED=false compatibility (bypassed auth keeps legacy behavior)
# =============================================================================


def test_disabled_search_keeps_user_fallback_and_reaches_handler(
    entry_module, seeded, guards, make_client
):
    client = make_client(BYPASSED_AUTH)

    response = client.post(
        "/product/search", json={"query": "q", "user_id": "legacy-user"}
    )

    assert response.status_code == 200
    guards["search"]["view"].search_memories.assert_called_once()
    # Legacy fallback: no cubes -> the body user id, unchanged.
    assert guards["search"]["record"] == [(["legacy-user"], "legacy-user")]


def test_disabled_delete_keeps_legacy_call_shape(entry_module, seeded, guards, make_client):
    from memos.api.routers import server_router

    client = make_client(BYPASSED_AUTH)

    response = client.post("/product/delete_memory", json={"user_id": "legacy-user"})

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "success"
    server_router.naive_mem_cube.text_mem.delete_by_filter.assert_called_once_with(
        writable_cube_ids=None,
        filter={"and": [{"user_id": "legacy-user"}]},
    )


def test_disabled_get_memory_by_ids_ignores_missing_metadata(
    entry_module, seeded, guards, make_client
):
    from memos.api.routers import server_router

    server_router.naive_mem_cube.text_mem.get_by_ids.return_value = [_memory_doc("m1", None)]
    client = make_client(BYPASSED_AUTH)

    response = client.post("/product/get_memory_by_ids", json=["m1"])

    assert response.status_code == 200
    assert len(response.json()["data"]["memories"]) == 1


def test_disabled_record_delete_keeps_graph_call(entry_module, seeded, guards, make_client):
    from memos.api.routers import server_router

    client = make_client(BYPASSED_AUTH)

    response = client.post(
        "/product/delete_memory_by_record_id",
        json={"mem_cube_id": "whatever-cube", "record_id": "record-9"},
    )

    assert response.status_code == 200
    server_router.graph_db.delete_node_by_mem_cube_id.assert_called_once_with(
        mem_cube_id="whatever-cube", delete_record_id="record-9", hard_delete=False
    )


# =============================================================================
# Streams: unauthorized access must be an initial HTTP 403, never a 200 that
# later emits an error SSE event (plan 5.3 #15/#18).
# =============================================================================


def test_unauthorized_chat_stream_is_initial_403(entry_module, seeded, guards, make_client):
    client = make_client(regular_auth("mallory"))

    with client.stream("POST", "/product/chat/stream", json={
        "user_id": "mallory-id",
        "query": "q",
        "readable_cube_ids": ["alice-cube"],
    }) as response:
        # The denial must be visible before any SSE body byte is produced.
        assert response.status_code == 403
        body = response.read()
        assert body == b'{"detail":"Insufficient cube access"}'


def test_unauthorized_scheduler_wait_stream_is_initial_403(
    entry_module, seeded, guards, make_client
):
    client = make_client(regular_auth("mallory"))

    with client.stream("GET", "/product/scheduler/wait/stream", params={"user_name": "alice-id"}) as response:
        assert response.status_code == 403
        body = response.read()
        assert body == b'{"detail":"Insufficient cube access"}'


def test_authorized_chat_stream_owner_passes(entry_module, seeded, guards, make_client):
    client = make_client(regular_auth("alice"))

    with client.stream("POST", "/product/chat/stream", json={
        "user_id": seeded["alice_id"],
        "query": "q",
        "readable_cube_ids": ["alice-cube"],
    }) as response:
        assert response.status_code == 200
