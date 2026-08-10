"""
Auth middleware failure-mode tests (auth-hardening tasks 1 + 2).

All tests patch module constants via monkeypatch and build raw Starlette
request scopes; no real database or .env is involved.
"""

import hashlib
import importlib

from datetime import UTC, datetime, timedelta

import pytest

from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from memos.api.middleware import auth
from memos.context.context import RequestContext, get_current_user_name, set_request_context


EXTERNAL_CLIENT = ("203.0.113.7", 40000)
MASTER_KEY = "mk_test_master_key_0123456789"
MASTER_KEY_HASH = hashlib.sha256(MASTER_KEY.encode()).hexdigest()
REGULAR_KEY = "krlk_" + "ab" * 32  # canonical krlk_<64hex> format


def make_request(headers: dict[str, str] | None = None, client=EXTERNAL_CLIENT) -> Request:
    """Build a Starlette Request from a raw ASGI scope."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _restore_module_constants():
    """Restore module-level constants mutated (or reloaded) by a test."""
    saved = {
        name: getattr(auth, name)
        for name in ("AUTH_ENABLED", "MASTER_KEY_HASH", "INTERNAL_SERVICE_SECRET")
    }
    yield
    for name, value in saved.items():
        setattr(auth, name, value)


@pytest.fixture()
def auth_enabled(monkeypatch):
    """Auth enabled with no master key / internal secret unless a test sets them."""
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "MASTER_KEY_HASH", None)
    monkeypatch.setattr(auth, "INTERNAL_SERVICE_SECRET", None)
    return auth


# 1. Default must fail closed when AUTH_ENABLED is unset.
def test_auth_defaults_to_enabled_when_env_missing(monkeypatch):
    import dotenv

    # Neutralize any .env so only the os.getenv default is observed.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("AUTH_ENABLED", raising=False)

    importlib.reload(auth)

    assert auth.AUTH_ENABLED is True


# 2. Explicit opt-out keeps local deployments working.
async def test_explicit_auth_disabled_bypasses_auth(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)

    result = await auth.verify_api_key(make_request(), None)

    assert result == {
        "user_name": "default",
        "scopes": ["all"],
        "is_master_key": False,
        "auth_bypassed": True,
    }


# 3. Enabled + external IP + no key -> 401 with ApiKey challenge.
async def test_missing_key_returns_401_when_enabled(auth_enabled):
    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_api_key(make_request(), None)

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Missing API key"
    assert exc_info.value.headers["WWW-Authenticate"] == "ApiKey"


# 4. Malformed Authorization value must be rejected before any DB access.
async def test_invalid_key_format_returns_401(auth_enabled, monkeypatch):
    async def _fail_lookup(key_hash):
        raise AssertionError("lookup_api_key must not be called for an invalid key format")

    monkeypatch.setattr(auth, "lookup_api_key", _fail_lookup)

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_api_key(make_request(), "not-a-key")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid API key format"


# 5. Master key via Bearer prefix.
async def test_bearer_master_key_is_accepted(auth_enabled, monkeypatch):
    monkeypatch.setattr(auth, "MASTER_KEY_HASH", MASTER_KEY_HASH)

    result = await auth.verify_api_key(make_request(), f"Bearer {MASTER_KEY}")

    assert result["is_master_key"] is True
    assert result["user_name"] == "admin"
    assert result["scopes"] == ["all"]


# 6. Master key via legacy Token prefix.
async def test_token_master_key_is_accepted(auth_enabled, monkeypatch):
    monkeypatch.setattr(auth, "MASTER_KEY_HASH", MASTER_KEY_HASH)

    result = await auth.verify_api_key(make_request(), f"Token {MASTER_KEY}")

    assert result["is_master_key"] is True


# 7. Canonical regular key accepted via patched lookup.
async def test_regular_key_is_accepted(auth_enabled, monkeypatch):
    async def _lookup(key_hash):
        assert key_hash == auth.hash_api_key(REGULAR_KEY)
        return {"id": "key-1", "user_name": "alice", "scopes": ["read", "write"]}

    monkeypatch.setattr(auth, "lookup_api_key", _lookup)

    result = await auth.verify_api_key(make_request(), f"Bearer {REGULAR_KEY}")

    assert result["user_name"] == "alice"
    assert result["scopes"] == ["read", "write"]
    assert result["is_master_key"] is False
    assert result["api_key_id"] == "key-1"


# 8. Unknown regular key -> 401.
async def test_unknown_regular_key_returns_401(auth_enabled, monkeypatch):
    async def _lookup(key_hash):
        return None

    monkeypatch.setattr(auth, "lookup_api_key", _lookup)

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_api_key(make_request(), f"Bearer {REGULAR_KEY}")

    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "Invalid or expired API key"


# 9. Revoked and expired keys hit the same unified runtime rejection.
@pytest.mark.parametrize("scenario", ["revoked", "expired"])
async def test_revoked_or_expired_key_returns_401(auth_enabled, monkeypatch, scenario):
    async def _lookup(key_hash):
        # Lookup already filters revoked/expired rows; None means rejected.
        return None

    monkeypatch.setattr(auth, "lookup_api_key", _lookup)

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_api_key(make_request(), f"Bearer {REGULAR_KEY}")

    assert exc_info.value.status_code == 401


# 10. Unset secret + missing header must NOT authenticate as internal
# (guards the historical `None == None` hole).
def test_missing_internal_header_and_unset_secret_is_not_internal(auth_enabled):
    assert auth.is_internal_request(make_request()) is False

    with_header = make_request(headers={"X-Internal-Service": "anything"})
    assert auth.is_internal_request(with_header) is False


# 11. Correct non-empty shared secret from an external IP stays supported.
async def test_untrusted_source_with_matching_internal_secret_is_accepted(
    auth_enabled, monkeypatch
):
    monkeypatch.setattr(auth, "INTERNAL_SERVICE_SECRET", "shared-secret")
    request = make_request(headers={"X-Internal-Service": "shared-secret"})

    assert auth.is_internal_request(request) is True

    result = await auth.verify_api_key(request, None)
    assert result["is_internal"] is True
    assert result["user_name"] == "internal"


# 12. Wrong secret falls through to normal key validation -> 401.
async def test_untrusted_source_with_wrong_internal_secret_is_rejected(
    auth_enabled, monkeypatch
):
    monkeypatch.setattr(auth, "INTERNAL_SERVICE_SECRET", "shared-secret")
    request = make_request(headers={"X-Internal-Service": "wrong-secret"})

    assert auth.is_internal_request(request) is False

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_api_key(request, None)
    assert exc_info.value.status_code == 401


# 13. Trusted internal hosts need no header (Hermes compatibility).
@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "memos-mcp"])
async def test_trusted_internal_service_is_accepted_without_header(auth_enabled, host):
    request = make_request(client=(host, 40000))

    assert auth.is_internal_request(request) is True

    result = await auth.verify_api_key(request, None)
    assert result["is_internal"] is True
    assert result["user_name"] == "internal"


# 14. Auth database outage must fail closed, never open.
async def test_database_unavailable_fails_closed(auth_enabled, monkeypatch):
    monkeypatch.setattr(auth, "_get_auth_pool", lambda: None)

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_api_key(make_request(), f"Bearer {REGULAR_KEY}")

    assert exc_info.value.status_code == 401


# 15. lookup_api_key SQL behavior via fake pool/connection/cursor.
class FakeCursor:
    def __init__(self, row):
        self._row = row
        self.executed: list[tuple[str, tuple | None]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeConnection:
    def __init__(self, row):
        self.cursor_obj = FakeCursor(row)
        self.commits = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1


class FakePool:
    def __init__(self, row):
        self.conn = FakeConnection(row)
        self.returned: list = []

    def getconn(self):
        return self.conn

    def putconn(self, conn):
        self.returned.append(conn)


async def test_lookup_rejects_inactive_key(monkeypatch):
    pool = FakePool(row=("key-1", "alice", ["read"], None, False))
    monkeypatch.setattr(auth, "_get_auth_pool", lambda: pool)

    assert await auth.lookup_api_key("some-hash") is None

    ((select_sql, select_params),) = pool.conn.cursor_obj.executed
    assert "FROM api_keys" in select_sql
    assert "key_hash = %s" in select_sql
    assert select_params == ("some-hash",)


async def test_lookup_rejects_expired_key(monkeypatch):
    # PostgreSQL TIMESTAMPTZ comes back as an aware datetime.
    expired_at = datetime.now(UTC) - timedelta(days=1)
    pool = FakePool(row=("key-1", "alice", ["read"], expired_at, True))
    monkeypatch.setattr(auth, "_get_auth_pool", lambda: pool)

    assert await auth.lookup_api_key("some-hash") is None
    # No last_used_at update for a rejected key.
    assert len(pool.conn.cursor_obj.executed) == 1
    assert pool.conn.commits == 0


async def test_lookup_accepts_unexpired_key_and_updates_last_used(monkeypatch):
    expires_at = datetime.now(UTC) + timedelta(days=1)
    pool = FakePool(row=("key-1", "alice", ["read", "write"], expires_at, True))
    monkeypatch.setattr(auth, "_get_auth_pool", lambda: pool)

    result = await auth.lookup_api_key("some-hash")

    assert result == {"id": "key-1", "user_name": "alice", "scopes": ["read", "write"]}

    update_sql, update_params = pool.conn.cursor_obj.executed[-1]
    assert "UPDATE api_keys SET last_used_at" in update_sql
    assert update_params == ("key-1",)
    assert pool.conn.commits == 1
    assert pool.returned == [pool.conn]  # connection returned to the pool


# 17. load_dotenv() must run before the constants are initialized.
def test_auth_configuration_loads_dotenv_before_constants(monkeypatch, tmp_path):
    import dotenv

    env_file = tmp_path / ".env"  # no real credentials
    fake_hash = "f" * 64
    env_file.write_text(f"AUTH_ENABLED=false\nMASTER_KEY_HASH={fake_hash}\n", encoding="utf-8")

    real_load_dotenv = dotenv.load_dotenv

    def _load_dotenv(*args, **kwargs):
        kwargs.setdefault("dotenv_path", env_file)
        return real_load_dotenv(*args, **kwargs)

    monkeypatch.setattr(dotenv, "load_dotenv", _load_dotenv)
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.delenv("MASTER_KEY_HASH", raising=False)

    importlib.reload(auth)

    # If constants were computed before load_dotenv(), AUTH_ENABLED would
    # still be the (now fail-closed) default True and the hash would be None.
    assert auth.AUTH_ENABLED is False
    assert fake_hash == auth.MASTER_KEY_HASH


# ---------------------------------------------------------------------------
# P0-1: authenticated identity must be published to request.state and the
# request context (plan section 3.1).
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded_context():
    """Ensure the request context is cleared after tests that seed it."""
    yield
    set_request_context(None)


# 16. Successful authentication publishes identity to request.state.
async def test_verify_api_key_injects_auth_into_request_state(auth_enabled, monkeypatch):
    async def _lookup(key_hash):
        return {"id": "key-1", "user_name": "alice", "scopes": ["read", "write"]}

    monkeypatch.setattr(auth, "lookup_api_key", _lookup)
    request = make_request()

    result = await auth.verify_api_key(request, f"Bearer {REGULAR_KEY}")

    assert result == {
        "user_name": "alice",
        "scopes": ["read", "write"],
        "is_master_key": False,
        "api_key_id": "key-1",
    }
    assert request.state.auth == result
    assert request.state.user == "alice"


# 17. Authenticated user overrides a spoofed X-User-Name header in the context.
async def test_authenticated_user_overrides_spoofed_header_in_context(
    auth_enabled, monkeypatch, seeded_context
):
    async def _lookup(key_hash):
        return {"id": "key-1", "user_name": "alice", "scopes": ["read"]}

    monkeypatch.setattr(auth, "lookup_api_key", _lookup)

    # RequestContextMiddleware seeds user_name from the (spoofable) header
    # before authentication runs.
    set_request_context(RequestContext(user_name="victim"))
    request = make_request(headers={"X-User-Name": "victim"})

    result = await auth.verify_api_key(request, f"Bearer {REGULAR_KEY}")

    assert result["user_name"] == "alice"
    assert request.state.user == "alice"
    assert get_current_user_name() == "alice"


# 18. get_current_user re-publishes even when verify_api_key is overridden.
def test_get_current_user_publishes_dependency_override_result():
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(request: Request, current_user: dict = Depends(auth.get_current_user)):
        return {
            "user": current_user["user_name"],
            "state_user": request.state.user,
            "state_auth": request.state.auth,
        }

    override_auth = {"user_name": "override-user", "scopes": ["all"], "is_master_key": False}
    app.dependency_overrides[auth.verify_api_key] = lambda: override_auth

    with TestClient(app) as client:
        response = client.get("/whoami")

    assert response.status_code == 200
    assert response.json() == {
        "user": "override-user",
        "state_user": "override-user",
        "state_auth": override_auth,
    }


# 19. AUTH_ENABLED=false keeps header identity (or "default") and the bypass flag.
async def test_auth_disabled_keeps_header_identity(monkeypatch, seeded_context):
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)

    # With an X-User-Name header the header identity is kept.
    set_request_context(RequestContext(user_name="bob"))
    request = make_request(headers={"X-User-Name": "bob"})
    result = await auth.verify_api_key(request, None)

    assert result["auth_bypassed"] is True
    assert result["user_name"] == "bob"
    assert request.state.user == "bob"
    assert get_current_user_name() == "bob"

    # Without the header the identity falls back to "default".
    set_request_context(RequestContext(user_name="default"))
    request_no_header = make_request()
    result_no_header = await auth.verify_api_key(request_no_header, None)

    assert result_no_header["auth_bypassed"] is True
    assert result_no_header["user_name"] == "default"
    assert request_no_header.state.user == "default"
    assert get_current_user_name() == "default"


# 20. Master and internal short-circuit branches also publish state/context.
async def test_master_and_internal_auth_are_published(auth_enabled, monkeypatch, seeded_context):
    monkeypatch.setattr(auth, "MASTER_KEY_HASH", MASTER_KEY_HASH)
    set_request_context(RequestContext(user_name="victim"))

    master_request = make_request()
    master_result = await auth.verify_api_key(master_request, f"Bearer {MASTER_KEY}")

    assert master_result["is_master_key"] is True
    assert master_request.state.auth == master_result
    assert master_request.state.user == "admin"
    assert get_current_user_name() == "admin"

    internal_request = make_request(client=("127.0.0.1", 40000))
    internal_result = await auth.verify_api_key(internal_request, None)

    assert internal_result["is_internal"] is True
    assert internal_request.state.auth == internal_result
    assert internal_request.state.user == "internal"
    assert get_current_user_name() == "internal"
