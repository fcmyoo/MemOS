"""User self-service routes: /me/profile and /me/keys (batch 4).

Covers: profile read (user + default cube + key count), password change with
current-password gate (which revokes all sessions), self-service API-key
creation/listing/revocation with server-forced ownership and uniform 403 on
cross-user revoke. The PostgreSQL key store is mocked at the router boundary.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memos.api.routers.auth_router import (
    AuthServices,
    WebAuthHTTPError,
    router as auth_router,
    set_auth_services,
    web_auth_error_handler,
)
from memos.api.routers.me_router import router as me_router
from memos.api.web_auth import (
    ACCESS_TOKEN_PREFIX,
    FakeClock,
    SessionService,
    WebPasswordService,
    WebRateLimiter,
    WebSessionStore,
    WebTokenService,
)
from memos.mem_user.user_manager import UserManager

EPOCH = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
PASSWORD = "correct-horse-battery"


class _FakeArgon2Hasher:
    def hash(self, password: str) -> str:
        return f"$argon2id$v=19$fake${hashlib.sha256(password.encode()).hexdigest()}"

    def verify(self, password_hash: str, password: str) -> bool:
        expected = f"$argon2id$v=19$fake${hashlib.sha256(password.encode()).hexdigest()}"
        if password_hash != expected:
            raise ValueError("password does not match")
        return True

    def check_needs_rehash(self, password_hash: str) -> bool:
        return False


class _FakeCursor:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.rowcount = 0
        self._executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._executed.append((sql, params))
        return self

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _FakeConn:
    """Minimal psycopg2-like connection for the mocked key store."""

    def __init__(self):
        self.cursor_obj = _FakeCursor()
        self.committed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def close(self):
        pass


@pytest.fixture()
def auth_env(tmp_path, monkeypatch):
    monkeypatch.delenv("REGISTRATION_MODE", raising=False)

    clock = FakeClock(start=EPOCH)
    db_path = str(tmp_path / "memos_users.db")
    user_manager = UserManager(db_path=db_path)
    store = WebSessionStore(db_path=db_path, clock=clock)
    tokens = WebTokenService(clock=clock)
    services = AuthServices(
        user_manager=user_manager,
        session_service=SessionService(
            store=store,
            tokens=tokens,
            is_user_active=user_manager.validate_user,
            clock=clock,
        ),
        passwords=WebPasswordService(hasher=_FakeArgon2Hasher()),
        rate_limiter=WebRateLimiter(clock=clock),
        clock=clock,
    )
    set_auth_services(services)

    app = FastAPI()
    app.add_exception_handler(WebAuthHTTPError, web_auth_error_handler)
    app.include_router(auth_router)
    app.include_router(me_router)

    yield SimpleNamespace(
        client=TestClient(app),
        clock=clock,
        services=services,
        user_manager=user_manager,
        store=store,
    )

    set_auth_services(None)
    user_manager.close()


def _register(client, user_name="alice", password=PASSWORD):
    return client.post("/auth/register", json={"user_name": user_name, "password": password})


def _register_and_tokens(client, user_name="alice"):
    response = _register(client, user_name=user_name)
    assert response.status_code == 201, response.text
    body = response.json()
    return body["access_token"], body["refresh_token"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _mock_db(monkeypatch, fake_conn=None):
    """Point me_router._get_db_connection at a fake connection."""
    conn = fake_conn or _FakeConn()
    monkeypatch.setattr("memos.api.routers.me_router._get_db_connection", lambda: conn)
    return conn


# ---------------------------------------------------------------------------
# GET /me/profile
# ---------------------------------------------------------------------------


class TestProfile:
    def test_returns_user_default_cube_and_key_count(self, auth_env, monkeypatch):
        access, _ = _register_and_tokens(auth_env.client)
        conn = _FakeConn()
        conn.cursor_obj.rows = [
            ("k1", "krlk_abcd", "alice", ["read"], "test", None, None, None, True)
        ]
        monkeypatch.setattr("memos.api.routers.me_router._get_db_connection", lambda: conn)

        response = auth_env.client.get("/me/profile", headers=_bearer(access))
        assert response.status_code == 200
        body = response.json()
        assert body["user"]["user_name"] == "alice"
        assert body["user"]["role"] == "USER"
        assert body["default_cube"]["cube_name"] == "alice"
        assert body["key_count"] == 1
        assert "password" not in body["user"]

    def test_missing_bearer_401(self, auth_env):
        assert auth_env.client.get("/me/profile").status_code == 401

    def test_refresh_token_cannot_access_profile(self, auth_env):
        _, refresh = _register_and_tokens(auth_env.client)
        assert auth_env.client.get("/me/profile", headers=_bearer(refresh)).status_code == 401


# ---------------------------------------------------------------------------
# PATCH /me/profile — change password
# ---------------------------------------------------------------------------


class TestChangePassword:
    def test_wrong_current_password_401(self, auth_env):
        access, _ = _register_and_tokens(auth_env.client)
        response = auth_env.client.patch(
            "/me/profile",
            headers=_bearer(access),
            json={"current_password": "wrong-pass-long", "new_password": "new-correct-pass1"},
        )
        assert response.status_code == 401

    def test_success_changes_password_and_revokes_sessions(self, auth_env):
        access, refresh = _register_and_tokens(auth_env.client)
        response = auth_env.client.patch(
            "/me/profile",
            headers=_bearer(access),
            json={"current_password": PASSWORD, "new_password": "brand-new-pass-99"},
        )
        assert response.status_code == 204

        # Old session is dead (all sessions revoked).
        assert auth_env.client.get("/me/profile", headers=_bearer(access)).status_code == 401
        # New password works.
        login = auth_env.client.post(
            "/auth/login", json={"user_name": "alice", "password": "brand-new-pass-99"}
        )
        assert login.status_code == 200

    def test_weak_new_password_422(self, auth_env):
        access, _ = _register_and_tokens(auth_env.client)
        response = auth_env.client.patch(
            "/me/profile",
            headers=_bearer(access),
            json={"current_password": PASSWORD, "new_password": "short"},
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /me/keys — metadata only
# ---------------------------------------------------------------------------


class TestListMyKeys:
    def test_returns_only_own_metadata(self, auth_env, monkeypatch):
        access, _ = _register_and_tokens(auth_env.client)
        conn = _FakeConn()
        conn.cursor_obj.rows = [
            ("k1", "krlk_abcd", "alice", ["read"], "d", None, None, None, True)
        ]
        monkeypatch.setattr("memos.api.routers.me_router._get_db_connection", lambda: conn)

        response = auth_env.client.get("/me/keys", headers=_bearer(access))
        assert response.status_code == 200
        keys = response.json()
        assert len(keys) == 1
        assert keys[0]["key_prefix"] == "krlk_abcd"
        # Full key material never appears.
        assert "key" not in keys[0]

    def test_unauthorized_401(self, auth_env):
        assert auth_env.client.get("/me/keys").status_code == 401


# ---------------------------------------------------------------------------
# POST /me/keys — owner forced server-side
# ---------------------------------------------------------------------------


class TestCreateMyKey:
    def test_creates_key_with_server_owner(self, auth_env, monkeypatch):
        access, _ = _register_and_tokens(auth_env.client)
        conn = _FakeConn()
        monkeypatch.setattr("memos.api.routers.me_router._get_db_connection", lambda: conn)

        response = auth_env.client.post(
            "/me/keys",
            headers=_bearer(access),
            json={"scopes": ["read"], "description": "my key"},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["key"].startswith("krlk_")
        assert "key" in body  # full key exactly once at creation
        # Owner was taken from the session, not the body.
        created = [c for c in conn.cursor_obj._executed if "INSERT INTO api_keys" in c[0]]
        assert created, "expected an INSERT into api_keys"
        assert "alice" in created[0][1]

    def test_owner_never_from_body(self, auth_env, monkeypatch):
        access, _ = _register_and_tokens(auth_env.client)
        conn = _FakeConn()
        monkeypatch.setattr("memos.api.routers.me_router._get_db_connection", lambda: conn)

        response = auth_env.client.post(
            "/me/keys",
            headers=_bearer(access),
            json={"scopes": ["read"], "user_name": "mallory"},
        )
        assert response.status_code == 201
        created = [c for c in conn.cursor_obj._executed if "INSERT INTO api_keys" in c[0]]
        assert "alice" in created[0][1]
        assert "mallory" not in created[0][1]


# ---------------------------------------------------------------------------
# DELETE /me/keys/{key_id} — owner-conditioned atomic revoke, uniform 403
# ---------------------------------------------------------------------------


class TestRevokeMyKey:
    def test_success_revokes_own_key(self, auth_env, monkeypatch):
        access, _ = _register_and_tokens(auth_env.client)
        conn = _FakeConn()
        monkeypatch.setattr("memos.api.routers.me_router._get_db_connection", lambda: conn)

        def fake_revoke(conn_, key_id, user_name):
            return user_name == "alice" and key_id == "k123"

        monkeypatch.setattr(
            "memos.api.routers.me_router.revoke_api_key_for_user", fake_revoke
        )

        response = auth_env.client.delete("/me/keys/k123", headers=_bearer(access))
        assert response.status_code == 204

    def test_foreign_or_unknown_key_uniform_403(self, auth_env, monkeypatch):
        access, _ = _register_and_tokens(auth_env.client)
        conn = _FakeConn()
        monkeypatch.setattr("memos.api.routers.me_router._get_db_connection", lambda: conn)

        def fake_revoke(conn_, key_id, user_name):
            return False  # not yours / not found / already revoked

        monkeypatch.setattr(
            "memos.api.routers.me_router.revoke_api_key_for_user", fake_revoke
        )

        response = auth_env.client.delete("/me/keys/k999", headers=_bearer(access))
        assert response.status_code == 403
        assert response.json()["detail"] == "key_not_available"

    def test_unauthorized_401(self, auth_env):
        assert auth_env.client.delete("/me/keys/k1").status_code == 401
