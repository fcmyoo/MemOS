"""Tests for admin router endpoints and health endpoint behavior."""

from unittest.mock import Mock, patch

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from memos.api.middleware import auth as auth_module
from memos.api.routers import admin_router
from memos.api.utils.api_keys import APIKey


@pytest.fixture
def app() -> FastAPI:
    """Build a lightweight app with admin router for endpoint testing."""
    test_app = FastAPI()
    test_app.include_router(admin_router.router)
    return test_app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """Create TestClient for admin router tests."""
    return TestClient(app)


@pytest.fixture
def admin_auth_override(app: FastAPI):
    """Override verify_api_key dependency with admin-level scopes."""

    async def _override_verify_api_key():
        return {"user_name": "admin", "scopes": ["all"], "is_master_key": True}

    app.dependency_overrides[auth_module.verify_api_key] = _override_verify_api_key
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def read_only_auth_override(app: FastAPI):
    """Override verify_api_key dependency with read-only scopes."""

    async def _override_verify_api_key():
        return {"user_name": "reader", "scopes": ["read"], "is_master_key": False}

    app.dependency_overrides[auth_module.verify_api_key] = _override_verify_api_key
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def mock_db_connection():
    """Provide a mock DB connection used by admin router internals."""
    conn = Mock()
    conn.close = Mock()
    return conn


def test_create_key_requires_admin_scope(client: TestClient, read_only_auth_override):
    """Creating API key should be rejected when caller lacks admin scope."""
    response = client.post(
        "/admin/keys",
        json={"user_name": "alice", "scopes": ["read"], "description": "for read"},
    )

    assert response.status_code == 403


def test_create_key_success(
    client: TestClient,
    admin_auth_override,
    mock_db_connection: Mock,
):
    """Admin scope should allow creating an API key and return plaintext key once."""
    with (
        patch("memos.api.routers.admin_router._get_db_connection", return_value=mock_db_connection),
        patch(
            "memos.api.routers.admin_router.create_api_key_in_db",
            return_value=APIKey(
                key="krlk_" + "a" * 64,
                key_hash="h" * 64,
                key_prefix="krlk_aaaaaaaa",
            ),
        ),
    ):
        response = client.post(
            "/admin/keys",
            json={"user_name": "alice", "scopes": ["read", "write"], "description": "biz key"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_name"] == "alice"
    assert payload["key"].startswith("krlk_")
    assert payload["scopes"] == ["read", "write"]


def test_list_keys_returns_no_plaintext(
    client: TestClient,
    admin_auth_override,
    mock_db_connection: Mock,
):
    """Listing keys should not expose plaintext key values."""
    with (
        patch("memos.api.routers.admin_router._get_db_connection", return_value=mock_db_connection),
        patch(
            "memos.api.routers.admin_router.list_api_keys",
            return_value=[
                {
                    "id": "1",
                    "key_prefix": "krlk_abcd1234",
                    "user_name": "alice",
                    "scopes": ["read"],
                    "description": "read key",
                    "is_active": True,
                }
            ],
        ),
    ):
        response = client.get("/admin/keys")

    assert response.status_code == 200
    payload = response.json()
    assert "keys" in payload
    assert payload["keys"]
    assert "key" not in payload["keys"][0]


def test_revoke_key_success(
    client: TestClient,
    admin_auth_override,
    mock_db_connection: Mock,
):
    """Revoke endpoint should return success when underlying revoke operation succeeds."""
    with (
        patch("memos.api.routers.admin_router._get_db_connection", return_value=mock_db_connection),
        patch("memos.api.routers.admin_router.revoke_api_key", return_value=True),
    ):
        response = client.delete("/admin/keys/abc-123")

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True


def test_health_endpoint_no_auth():
    """Global health endpoint should be publicly accessible without auth."""
    mock_components = {
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

    with patch("memos.api.handlers.init_server", return_value=mock_components):
        from memos.api import server_api

        test_client = TestClient(server_api.app)
        response = test_client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert "auth_enabled" in payload
