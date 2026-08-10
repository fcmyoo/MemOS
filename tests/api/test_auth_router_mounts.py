"""
Unified-entrypoint plan stage A (failing tests): the single ``server_api`` app
must own auth, admin, rate limiting, CORS and security headers.

Stage A only adds/reshapes tests; ``src/memos`` is untouched, so most of these
fail until stage B lands:

* ``server_api`` gains the admin router, the CORS/security/rate-limit
  middleware stack and a fingerprint-free ``/health``;
* ``server_api_ext`` degrades to a compatibility shim whose ``app`` **is**
  ``server_api.app``;
* ``SecurityHeadersMiddleware`` moves into the shared middleware package.

Patches ``memos.api.handlers.init_server`` with mock components (same set as
``tests/api/test_server_router.py``) so the app imports without starting the
real MemOS stack. ``app.dependency_overrides[verify_api_key]`` is used to
observe whether the auth dependency executes, so module-level ``AUTH_ENABLED``
state can never pollute those assertions.

Note on object identity: ``tests/api/test_auth.py`` reloads
``memos.api.middleware.auth``, which replaces every function object in that
module. The entry point binds its own ``verify_api_key`` reference at import
time (``from memos.api.middleware.auth import verify_api_key``), so overrides
must key on the entry module's attribute (``server_api.verify_api_key``), NOT
on a fresh top-level import — otherwise the override silently misses and the
real auth dependency runs (401 on every request).
"""

import importlib
import os
import sys

from collections import defaultdict
from unittest.mock import Mock, patch

import pytest

from fastapi import HTTPException
from fastapi.testclient import TestClient

from memos.api.product_models import SearchResponse


EXPECTED_PRODUCT_OPERATIONS = 25
SEARCH_BODY = {"query": "test query", "user_id": "test_user", "mem_cube_id": "test_cube"}
AUTH_OK = {"user_name": "tester", "scopes": ["all"], "is_master_key": False}

# Deterministic stand-ins for the deployment defaults: auth on, rate limiting
# on, empty CORS allow-list (same-origin only). Explicit values also shield the
# import-time decisions from whatever a local .env may carry.
DEFAULT_ENTRY_ENV = {
    "AUTH_ENABLED": "true",
    "RATE_LIMIT_ENABLED": "true",
    "CORS_ORIGINS": "",
}

# Exact expected stack, outer-most to inner-most (== add_middleware order
# reversed). Rate limiting sits before auth to absorb brute force.
EXPECTED_DEFAULT_MIDDLEWARE = [
    "CORSMiddleware",
    "SecurityHeadersMiddleware",
    "RateLimitMiddleware",
    "RequestContextMiddleware",
]
EXPECTED_MIDDLEWARE_WITHOUT_RATE_LIMIT = [
    "CORSMiddleware",
    "SecurityHeadersMiddleware",
    "RequestContextMiddleware",
]

ADMIN_PATHS = ("/admin/keys", "/admin/generate-master-key", "/admin/health")


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


def _fresh_import_server_api():
    """Re-import ``memos.api.server_api`` so import-time env decisions re-run.

    Uses ``importlib`` instead of ``from memos.api import server_api``: after
    ``sys.modules.pop`` the package attribute may still reference the previous
    module object, so a plain from-import can silently return stale state.
    """
    import importlib

    sys.modules.pop("memos.api.server_api", None)
    return importlib.import_module("memos.api.server_api")


def _assert_security_headers(response) -> None:
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "DENY"
    assert "Referrer-Policy" in response.headers
    assert "Permissions-Policy" in response.headers


@pytest.fixture(scope="module")
def entry_module():
    """Import the single entry point with init_server patched out.

    Yields the module, not just the app: it carries the exact
    ``verify_api_key`` object its router dependencies are bound to, which is
    the correct key for ``dependency_overrides`` regardless of any reload of
    ``memos.api.middleware.auth`` performed by other test files.
    """
    with (
        patch.dict(os.environ, DEFAULT_ENTRY_ENV),
        patch("memos.api.handlers.init_server", return_value=_mock_components()),
    ):
        yield _fresh_import_server_api()


@pytest.fixture(scope="module")
def cors_entry_module():
    """server_api re-imported with a configured CORS allow-list."""
    env = dict(DEFAULT_ENTRY_ENV, CORS_ORIGINS="http://test.example")
    with (
        patch.dict(os.environ, env),
        patch("memos.api.handlers.init_server", return_value=_mock_components()),
    ):
        yield _fresh_import_server_api()


@pytest.fixture(scope="module")
def no_ratelimit_entry_module():
    """server_api re-imported with RATE_LIMIT_ENABLED=false."""
    env = dict(DEFAULT_ENTRY_ENV, RATE_LIMIT_ENABLED="false")
    with (
        patch.dict(os.environ, env),
        patch("memos.api.handlers.init_server", return_value=_mock_components()),
    ):
        yield _fresh_import_server_api()


@pytest.fixture
def app_with_overrides(entry_module):
    """Entry app whose dependency_overrides are cleared after each test.

    Also yields the module's ``verify_api_key`` so callers override the exact
    dependency object the app was built with.
    """
    yield entry_module.app, entry_module.verify_api_key
    entry_module.app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _neutralize_rate_limit(monkeypatch):
    """Keep RateLimitMiddleware deterministic per test (no Redis, empty store)."""
    from memos.api.middleware import rate_limit

    monkeypatch.setattr(rate_limit, "_get_redis", lambda: None)
    monkeypatch.setattr(rate_limit, "_memory_store", defaultdict(list))


class TestUnifiedAuthMount:
    """The single entry point keeps the P0 auth contract."""

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

    def test_health_is_public_and_has_no_fingerprint(self, app_with_overrides):
        app, verify_api_key = app_with_overrides
        # If the auth dependency ran on /health this override would force a 401.
        app.dependency_overrides[verify_api_key] = _reject_auth

        response = TestClient(app).get("/health")

        assert response.status_code == 200
        health_op = app.openapi()["paths"]["/health"]["get"]
        assert not health_op.get("security")

        # Health stays public information: no version/service fingerprint.
        body = response.json()
        assert body.get("status") == "healthy"
        assert "version" not in body

    def test_all_product_operations_declare_api_key_security(self, entry_module):
        app = entry_module.app

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


class TestUnifiedEntryPoint:
    """Capabilities server_api must absorb from the former ext entry point."""

    def test_openapi_exposes_admin_paths(self, entry_module):
        spec = entry_module.app.openapi()
        for path in ADMIN_PATHS:
            assert path in spec["paths"], f"{path} is not mounted on server_api"

        admin_keys_post = spec["paths"]["/admin/keys"]["post"]
        assert any(
            "APIKeyHeader" in requirement
            for requirement in admin_keys_post.get("security", [])
        )

    def test_admin_keys_scope_enforced_before_body_validation(self, entry_module):
        from memos.api.routers import admin_router

        app = entry_module.app
        # Key on admin_router's own binding: require_scope() captured it when
        # the router module was defined.
        app.dependency_overrides[admin_router.verify_api_key] = lambda: {
            "user_name": "reader",
            "scopes": ["read"],
            "is_master_key": False,
        }
        try:
            response = TestClient(app).post("/admin/keys", json={})
            # Router-level dependency rejects before body validation: scope, not 422.
            assert response.status_code == 403

            app.dependency_overrides[admin_router.verify_api_key] = lambda: {
                "user_name": "boss",
                "scopes": ["admin"],
                "is_master_key": False,
            }
            response = TestClient(app).post("/admin/keys", json={})
            # Past the scope dependency; falls over on request-body validation.
            assert response.status_code == 422
        finally:
            app.dependency_overrides.clear()

    def test_default_middleware_stack_order(self, entry_module):
        names = [m.cls.__name__ for m in entry_module.app.user_middleware]
        assert names == EXPECTED_DEFAULT_MIDDLEWARE

    def test_middleware_stack_without_rate_limit(self, no_ratelimit_entry_module):
        names = [m.cls.__name__ for m in no_ratelimit_entry_module.app.user_middleware]
        assert names == EXPECTED_MIDDLEWARE_WITHOUT_RATE_LIMIT

    def test_rate_limit_trips_before_auth(self, entry_module, monkeypatch):
        from memos.api.middleware import rate_limit

        monkeypatch.setattr(rate_limit, "RATE_LIMIT", 2)

        auth_calls = {"count": 0}

        def reject():
            auth_calls["count"] += 1
            raise HTTPException(
                status_code=401,
                detail="Missing API key",
                headers={"WWW-Authenticate": "ApiKey"},
            )

        app = entry_module.app
        app.dependency_overrides[entry_module.verify_api_key] = reject
        try:
            client = TestClient(app)
            codes = [
                client.post("/product/search", json=SEARCH_BODY).status_code
                for _ in range(3)
            ]
        finally:
            app.dependency_overrides.clear()

        # Auth rejection stays 401 while under the limit...
        assert codes[:2] == [401, 401]
        # ...and flips to 429 once the threshold is reached: rate limiting runs
        # before auth instead of replacing it.
        assert codes[2] == 429
        # The third request must be short-circuited before the auth dependency.
        assert auth_calls["count"] == 2

    def test_security_headers_on_health_401_and_429(self, entry_module, monkeypatch):
        from memos.api.middleware import rate_limit

        monkeypatch.setattr(rate_limit, "RATE_LIMIT", 2)

        app = entry_module.app
        app.dependency_overrides[entry_module.verify_api_key] = _reject_auth
        try:
            client = TestClient(app)
            health = client.get("/health")
            rejected = client.post("/product/search", json=SEARCH_BODY)
            client.post("/product/search", json=SEARCH_BODY)
            limited = client.post("/product/search", json=SEARCH_BODY)
        finally:
            app.dependency_overrides.clear()

        assert health.status_code == 200
        assert rejected.status_code == 401
        assert limited.status_code == 429
        for response in (health, rejected, limited):
            _assert_security_headers(response)

    def test_cors_preflight_allows_configured_origin(self, cors_entry_module):
        response = TestClient(cors_entry_module.app).options(
            "/product/search",
            headers={
                "Origin": "http://test.example",
                "Access-Control-Request-Method": "POST",
            },
        )

        assert response.headers.get("access-control-allow-origin") == "http://test.example"

    def test_cors_preflight_default_rejects_foreign_origin(self, entry_module):
        response = TestClient(entry_module.app).options(
            "/product/search",
            headers={
                "Origin": "http://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )

        # Empty default allow-list never clears an arbitrary third-party origin.
        assert "access-control-allow-origin" not in response.headers
        assert response.status_code == 400

    def test_auth_disabled_bypass_unmounts_admin_keeps_protections(self, monkeypatch):
        """AUTH_ENABLED=false regression: re-import server_api with auth off.

        We patch ``auth.AUTH_ENABLED`` with monkeypatch instead of
        ``importlib.reload(auth)``: a reload swaps the module object identity,
        which breaks every other test module that imported ``verify_api_key``
        / ``get_current_user`` earlier (their from-imports keep pointing at the
        old objects, so dependency overrides silently stop matching).
        """
        from memos.api.middleware import auth as auth_module

        # Patch the module attribute (not the object identity) so the
        # conditional admin mount and verify_api_key see the same snapshot.
        monkeypatch.setattr(auth_module, "AUTH_ENABLED", False)
        monkeypatch.setenv("AUTH_ENABLED", "false")
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")

        with patch("memos.api.handlers.init_server", return_value=_mock_components()):
            # Observe (not alter) what the real dependency publishes.
            published = []
            real_publish = auth_module._publish_authenticated_user

            def spy_publish(request, auth_context):
                published.append(auth_context)
                return real_publish(request, auth_context)

            with patch.object(auth_module, "_publish_authenticated_user", spy_publish):
                server_api_module = _fresh_import_server_api()
                app = server_api_module.app

                with patch("memos.api.routers.server_router.search_handler") as mock_search:
                    mock_search.handle_search_memories.return_value = SearchResponse(
                        message="Search completed successfully",
                        data={"text_mem": [], "act_mem": [], "para_mem": []},
                    )
                    response = TestClient(app).post("/product/search", json=SEARCH_BODY)

                assert response.status_code == 200
                mock_search.handle_search_memories.assert_called_once()

                assert published, "verify_api_key never published an auth context"
                assert published[-1].get("auth_bypassed") is True

                # Admin router is not mounted at all: 404, not 401/403/422/500.
                assert TestClient(app).post("/admin/keys", json={}).status_code == 404
                assert TestClient(app).get("/admin/health").status_code == 404

                # Protective layers must survive AUTH_ENABLED=false.
                names = [m.cls.__name__ for m in app.user_middleware]
                assert "RateLimitMiddleware" in names
                _assert_security_headers(TestClient(app).get("/health"))

    def test_ext_module_is_compat_alias_for_server_api(self):
        """server_api_ext keeps existing as an importable shim, nothing more."""
        with (
            patch.dict(os.environ, DEFAULT_ENTRY_ENV),
            patch("memos.api.handlers.init_server", return_value=_mock_components()),
        ):
            sys.modules.pop("memos.api.server_api_ext", None)
            sys.modules.pop("memos.api.server_api", None)
            from memos.api import server_api, server_api_ext

        assert server_api_ext.app is server_api.app

        # The shim must not carry a second FastAPI instance or its own stack.
        fastapi_instances = {
            id(value)
            for value in vars(server_api_ext).values()
            if isinstance(value, type(server_api.app)) and value is not None
        }
        assert fastapi_instances == {id(server_api.app)}
