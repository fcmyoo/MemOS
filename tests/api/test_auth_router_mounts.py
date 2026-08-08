"""
Auth-hardening tasks 3+4: both API entry points mount auth uniformly.

Patches ``memos.api.handlers.init_server`` with mock components (same set as
``tests/api/test_server_router.py``) so both entry apps can be imported without
starting the real MemOS stack. ``app.dependency_overrides[verify_api_key]`` is
used to observe whether the auth dependency executes, so module-level
``AUTH_ENABLED`` state can never pollute these assertions.

Note on object identity: ``tests/api/test_auth.py`` reloads
``memos.api.middleware.auth``, which replaces every function object in that
module.  The entry points bind their own ``verify_api_key`` reference at import
time (``from memos.api.middleware.auth import verify_api_key``), so overrides
must key on the entry module's attribute (``server_api.verify_api_key``), NOT on
a fresh top-level import — otherwise the override silently misses and the real
auth dependency runs (401 on every request).
"""

from collections import defaultdict
from unittest.mock import Mock, patch

import pytest

from fastapi import HTTPException
from fastapi.testclient import TestClient

from memos.api.product_models import SearchResponse


EXPECTED_PRODUCT_OPERATIONS = 25
SEARCH_BODY = {"query": "test query", "user_id": "test_user", "mem_cube_id": "test_cube"}
AUTH_OK = {"user_name": "tester", "scopes": ["all"], "is_master_key": False}


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


def _reject_auth() -> dict:
    raise HTTPException(
        status_code=401,
        detail="Missing API key",
        headers={"WWW-Authenticate": "ApiKey"},
    )


@pytest.fixture(scope="module")
def entry_modules():
    """Import both entry-point modules with init_server patched out.

    Yields the modules, not just the apps: each module carries the exact
    ``verify_api_key`` object its router dependencies are bound to, which is the
    correct key for ``dependency_overrides`` regardless of any reload of
    ``memos.api.middleware.auth`` performed by other test files.
    """
    with patch("memos.api.handlers.init_server", return_value=_mock_components()):
        from memos.api import server_api, server_api_ext

        yield {"server_api": server_api, "server_api_ext": server_api_ext}


@pytest.fixture(params=["server_api", "server_api_ext"])
def entry(request, entry_modules):
    """Parametrize shared assertions across both entry points."""
    return request.param, entry_modules[request.param]


@pytest.fixture
def app_with_overrides(entry):
    """Entry app whose dependency_overrides are cleared after each test.

    Also yields the module's ``verify_api_key`` so callers override the exact
    dependency object the app was built with.
    """
    _name, mod = entry
    yield mod.app, mod.verify_api_key
    mod.app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _neutralize_rate_limit(monkeypatch):
    """Keep the ext entry point's RateLimitMiddleware deterministic per test."""
    from memos.api.middleware import rate_limit

    monkeypatch.setattr(rate_limit, "_get_redis", lambda: None)
    monkeypatch.setattr(rate_limit, "_memory_store", defaultdict(list))


@pytest.fixture
def ext_app(entry_modules):
    return entry_modules["server_api_ext"]


class TestUnifiedAuthMount:
    """Shared assertions: both entry points must behave identically."""

    def test_product_search_rejected_returns_401_and_skips_handler(self, app_with_overrides):
        app, verify_api_key = app_with_overrides
        app.dependency_overrides[verify_api_key] = _reject_auth

        with patch("memos.api.routers.server_router.search_handler") as mock_search:
            response = TestClient(app).post("/product/search", json=SEARCH_BODY)

        assert response.status_code == 401
        mock_search.handle_search_memories.assert_not_called()

    def test_product_search_reaches_handler_after_auth_passes(self, app_with_overrides):
        app, verify_api_key = app_with_overrides
        app.dependency_overrides[verify_api_key] = lambda: AUTH_OK

        with patch("memos.api.routers.server_router.search_handler") as mock_search:
            mock_search.handle_search_memories.return_value = SearchResponse(
                message="Search completed successfully",
                data={"text_mem": [], "act_mem": [], "para_mem": []},
            )
            response = TestClient(app).post("/product/search", json=SEARCH_BODY)

        assert response.status_code == 200
        mock_search.handle_search_memories.assert_called_once()

    def test_health_is_public_and_skips_auth_dependency(self, app_with_overrides):
        app, verify_api_key = app_with_overrides
        # If the auth dependency ran on /health this override would force a 401.
        app.dependency_overrides[verify_api_key] = _reject_auth

        response = TestClient(app).get("/health")

        assert response.status_code == 200
        health_op = app.openapi()["paths"]["/health"]["get"]
        assert not health_op.get("security")

    def test_all_product_operations_declare_api_key_security(self, entry):
        _name, mod = entry
        app = mod.app

        spec = app.openapi()
        security_schemes = spec.get("components", {}).get("securitySchemes", {})
        assert "APIKeyHeader" in security_schemes

        operations = [
            (path, method, op)
            for path, methods in spec["paths"].items()
            if path.startswith("/product/")
            for method, op in methods.items()
            if method in {"get", "post", "put", "delete", "patch"}
        ]
        assert len(operations) == EXPECTED_PRODUCT_OPERATIONS

        for path, method, op in operations:
            security = op.get("security", [])
            assert any("APIKeyHeader" in requirement for requirement in security), (
                f"{method.upper()} {path} is missing the API key security requirement"
            )


class TestExtendedEntryPoint:
    """server_api_ext-specific behavior on top of the shared mount."""

    def test_health_defaults_auth_enabled_true(self, ext_app, monkeypatch):
        monkeypatch.delenv("AUTH_ENABLED", raising=False)

        response = TestClient(ext_app.app).get("/health")

        assert response.status_code == 200
        assert response.json()["auth_enabled"] is True

    def test_admin_keys_still_requires_admin_scope(self, ext_app):
        from memos.api.routers import admin_router

        verify_api_key = admin_router.verify_api_key
        ext_app.app.dependency_overrides[verify_api_key] = lambda: {
            "user_name": "reader",
            "scopes": ["read"],
            "is_master_key": False,
        }
        try:
            response = TestClient(ext_app.app).post("/admin/keys", json={})
        finally:
            ext_app.app.dependency_overrides.clear()

        # Router-level dependency rejects before body validation: scope, not 422.
        assert response.status_code == 403

        admin_keys_op = ext_app.app.openapi()["paths"]["/admin/keys"]["post"]
        assert any(
            "APIKeyHeader" in requirement for requirement in admin_keys_op.get("security", [])
        )

    def test_admin_keys_passes_scope_check_with_admin_scope(self, ext_app):
        from memos.api.routers import admin_router

        verify_api_key = admin_router.verify_api_key
        ext_app.app.dependency_overrides[verify_api_key] = lambda: {
            "user_name": "boss",
            "scopes": ["admin"],
            "is_master_key": False,
        }
        try:
            response = TestClient(ext_app.app).post("/admin/keys", json={})
        finally:
            ext_app.app.dependency_overrides.clear()

        # Past the scope dependency; falls over on request-body validation instead.
        assert response.status_code == 422

    def test_rate_limit_enabled_but_does_not_replace_auth(self, ext_app, monkeypatch):
        from memos.api.middleware import rate_limit

        assert any(m.cls.__name__ == "RateLimitMiddleware" for m in ext_app.app.user_middleware)

        monkeypatch.setattr(rate_limit, "RATE_LIMIT", 2)

        verify_api_key = ext_app.verify_api_key
        ext_app.app.dependency_overrides[verify_api_key] = _reject_auth
        try:
            client = TestClient(ext_app.app)
            codes = [
                client.post("/product/search", json=SEARCH_BODY).status_code for _ in range(3)
            ]
        finally:
            ext_app.app.dependency_overrides.clear()

        # Auth rejection stays 401 while under the limit...
        assert codes[:2] == [401, 401]
        # ...and only flips to 429 once the rate-limit threshold is reached.
        assert codes[2] == 429
