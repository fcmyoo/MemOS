"""Tests for the Web console auth router (phases 3+4).

Covers, per docs/plans/web-console-session-design.md §3.3/3.4 and
docs/plans/web-console-revamp.md §4.2:

- POST /auth/register: public-by-default registration with mode switches
  (public/invite/admin_only), atomic user + default-Cube creation, Argon2id
  password hashing, token pair issuance, 409 on duplicate, 422 on weak
  password, IP+user-name rate limiting;
- POST /auth/login: unified 401 for unknown user / wrong password / no
  password / inactive user, independent family per login, rate limiting;
- POST /auth/refresh: rotation (both tokens change, rotation +1, absolute
  refresh expiry fixed), the 10-second grace window for lost responses,
  replay detection revoking the whole family, stable 401 error codes;
- POST /auth/logout: access bearer or refresh fallback, idempotent 204;
- GET /auth/me: access-only, rejects refresh tokens and API keys;
- server_api mounting: AUTH_ENABLED=true exposes /auth/*, false yields 404.

Services are injected (tmp SQLite, FakeClock, fake Argon2 hasher) so no real
time, hashing cost or deployment database is involved.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import sys

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from memos.api.routers.auth_router import (
    AuthServices,
    WebAuthHTTPError,
    router,
    set_auth_services,
    web_auth_error_handler,
)
from memos.api.web_auth import (
    ACCESS_TOKEN_PREFIX,
    ACCESS_TOKEN_TTL,
    PREVIOUS_REFRESH_GRACE,
    REFRESH_TOKEN_PREFIX,
    REFRESH_TOKEN_TTL,
    FakeClock,
    SessionService,
    WebPasswordService,
    WebRateLimiter,
    WebSessionStore,
    WebTokenService,
)
from memos.mem_user.user_manager import UserManager


EPOCH = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
PASSWORD = "correct-horse-battery"  # >= 12 chars, not on the common list


class _FakeArgon2Hasher:
    """Stand-in for argon2.PasswordHasher (optional dependency)."""

    def hash(self, password: str) -> str:
        return f"$argon2id$v=19$fake${hashlib.sha256(password.encode()).hexdigest()}"

    def verify(self, password_hash: str, password: str) -> bool:
        expected = f"$argon2id$v=19$fake${hashlib.sha256(password.encode()).hexdigest()}"
        if password_hash != expected:
            raise ValueError("password does not match")
        return True

    def check_needs_rehash(self, password_hash: str) -> bool:
        return False


@pytest.fixture()
def auth_env(tmp_path, monkeypatch, postgres_test_schema):
    """App with injected auth services; clock frozen at EPOCH."""
    monkeypatch.delenv("REGISTRATION_MODE", raising=False)
    monkeypatch.delenv("WEB_CONSOLE_INVITE_CODE", raising=False)

    clock = FakeClock(start=EPOCH)
    db_path = str(tmp_path / "memos_users.db")
    user_manager = UserManager(db_path=db_path)
    store = WebSessionStore(
        database_url=postgres_test_schema.url,
        schema=postgres_test_schema.schema,
        clock=clock,
    )
    postgres_test_schema.register(store.engine)
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
    app.include_router(router)

    yield SimpleNamespace(
        client=TestClient(app),
        clock=clock,
        services=services,
        user_manager=user_manager,
        store=store,
    )

    set_auth_services(None)
    user_manager.close()
    store.close()


def _register(client, user_name="alice", password=PASSWORD, invite_code=None):
    body = {"user_name": user_name, "password": password}
    if invite_code is not None:
        body["invite_code"] = invite_code
    return client.post("/auth/register", json=body)


def _login(client, user_name="alice", password=PASSWORD):
    return client.post("/auth/login", json={"user_name": user_name, "password": password})


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _register_and_tokens(client, user_name="alice"):
    response = _register(client, user_name=user_name)
    assert response.status_code == 201, response.text
    body = response.json()
    return body["access_token"], body["refresh_token"]


# ---------------------------------------------------------------------------
# POST /auth/register
# ---------------------------------------------------------------------------


class TestRegister:
    def test_success_201_returns_user_cube_and_token_pair(self, auth_env):
        response = _register(auth_env.client)

        assert response.status_code == 201
        body = response.json()
        assert body["user"]["user_name"] == "alice"
        assert body["user"]["role"] == "USER"
        assert "user_id" in body["user"]
        assert body["default_cube"]["cube_name"] == "alice"
        assert "cube_id" in body["default_cube"]
        assert body["token_type"] == "Bearer"
        assert body["access_token"].startswith(ACCESS_TOKEN_PREFIX)
        assert body["refresh_token"].startswith(REFRESH_TOKEN_PREFIX)
        assert body["rotation"] == 0
        assert body["access_expires_at"] == (EPOCH + ACCESS_TOKEN_TTL).isoformat()
        assert body["refresh_expires_at"] == (EPOCH + REFRESH_TOKEN_TTL).isoformat()

    def test_persists_user_with_argon2_hash_and_default_cube(self, auth_env):
        _register(auth_env.client, user_name="carol")

        user = auth_env.user_manager.get_user_by_name("carol")
        assert user is not None
        assert user.password_hash is not None
        assert user.password_hash.startswith("$argon2id$")
        assert PASSWORD not in user.password_hash

        cubes = auth_env.user_manager.get_user_cubes(user.user_id)
        assert len(cubes) == 1
        assert cubes[0].owner_id == user.user_id

    def test_response_leaks_no_password_material(self, auth_env):
        response = _register(auth_env.client)
        assert PASSWORD not in response.text
        assert "password_hash" not in response.text
        assert "password" not in response.json()["user"]

    def test_response_is_not_cacheable(self, auth_env):
        response = _register(auth_env.client)
        assert response.headers.get("Cache-Control") == "no-store"

    def test_duplicate_user_name_409(self, auth_env):
        assert _register(auth_env.client).status_code == 201
        response = _register(auth_env.client)
        assert response.status_code == 409
        assert response.json()["message"] == "user_name_taken"

    def test_admin_only_mode_returns_404(self, auth_env, monkeypatch):
        monkeypatch.setenv("REGISTRATION_MODE", "admin_only")
        response = _register(auth_env.client)
        assert response.status_code == 404
        assert response.json()["message"] == "registration_disabled"

    @pytest.mark.parametrize("code", [None, "wrong-code"])
    def test_invite_mode_requires_valid_code(self, auth_env, monkeypatch, code):
        monkeypatch.setenv("REGISTRATION_MODE", "invite")
        monkeypatch.setenv("WEB_CONSOLE_INVITE_CODE", "golden-ticket")
        response = _register(auth_env.client, invite_code=code)
        assert response.status_code == 403
        assert response.json()["message"] == "invalid_invite_code"

    def test_invite_mode_accepts_valid_code(self, auth_env, monkeypatch):
        monkeypatch.setenv("REGISTRATION_MODE", "invite")
        monkeypatch.setenv("WEB_CONSOLE_INVITE_CODE", "golden-ticket")
        response = _register(auth_env.client, invite_code="golden-ticket")
        assert response.status_code == 201

    @pytest.mark.parametrize("password", ["short1", "eleven-char", "x" * 129])
    def test_password_length_policy_422(self, auth_env, password):
        assert _register(auth_env.client, password=password).status_code == 422

    def test_common_password_rejected_422(self, auth_env):
        response = _register(auth_env.client, password="password1234")
        assert response.status_code == 422
        assert response.json()["message"] == "weak_password"

    @pytest.mark.parametrize("user_name", ["ab", "x" * 33, " leading", "-dash", "spa ce"])
    def test_user_name_policy_422(self, auth_env, user_name):
        assert _register(auth_env.client, user_name=user_name).status_code == 422

    def test_rate_limited_after_three_per_hour_429(self, auth_env):
        assert _register(auth_env.client).status_code == 201
        # Duplicate attempts still consume the rate budget (checked first).
        assert _register(auth_env.client).status_code == 409
        assert _register(auth_env.client).status_code == 409
        response = _register(auth_env.client)
        assert response.status_code == 429
        assert response.json()["message"] == "rate_limited"

    def test_role_cannot_be_supplied_by_client(self, auth_env):
        response = auth_env.client.post(
            "/auth/register",
            json={"user_name": "mallory", "password": PASSWORD, "role": "ROOT"},
        )
        # Extra fields are ignored; the account is always USER.
        assert response.status_code == 201
        user = auth_env.user_manager.get_user_by_name("mallory")
        assert user.role.value == "USER"


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------


class TestLogin:
    def test_success_returns_user_and_token_pair(self, auth_env):
        _register(auth_env.client)

        response = _login(auth_env.client)

        assert response.status_code == 200
        body = response.json()
        assert body["user"]["user_name"] == "alice"
        assert body["token_type"] == "Bearer"
        assert body["access_token"].startswith(ACCESS_TOKEN_PREFIX)
        assert body["refresh_token"].startswith(REFRESH_TOKEN_PREFIX)
        assert body["rotation"] == 0
        assert response.headers.get("Cache-Control") == "no-store"

    def test_wrong_password_unified_401(self, auth_env):
        _register(auth_env.client)
        response = _login(auth_env.client, password="another-long-pass")
        assert response.status_code == 401
        assert response.json()["message"] == "invalid_credentials"

    def test_unknown_user_unified_401(self, auth_env):
        response = _login(auth_env.client, user_name="ghost")
        assert response.status_code == 401
        assert response.json()["message"] == "invalid_credentials"

    def test_user_without_password_unified_401(self, auth_env):
        # The seeded root user has no password hash (API-key era account).
        response = _login(auth_env.client, user_name="root", password=PASSWORD)
        assert response.status_code == 401
        assert response.json()["message"] == "invalid_credentials"

    def test_inactive_user_unified_401(self, auth_env):
        _register(auth_env.client)
        user = auth_env.user_manager.get_user_by_name("alice")
        assert auth_env.user_manager.delete_user(user.user_id) is True
        response = _login(auth_env.client)
        assert response.status_code == 401
        assert response.json()["message"] == "invalid_credentials"

    def test_each_login_creates_independent_family(self, auth_env):
        _register(auth_env.client)
        first = _login(auth_env.client).json()
        second = _login(auth_env.client).json()

        # Logging out of the second session leaves the first one alive.
        logout = auth_env.client.post(
            "/auth/logout", headers=_bearer(second["access_token"])
        )
        assert logout.status_code == 204
        assert (
            auth_env.client.get("/auth/me", headers=_bearer(first["access_token"]))
            .status_code
            == 200
        )
        assert (
            auth_env.client.get("/auth/me", headers=_bearer(second["access_token"]))
            .status_code
            == 401
        )

    def test_rate_limited_after_ten_per_minute_429(self, auth_env):
        _register(auth_env.client)
        for _ in range(10):
            assert _login(auth_env.client, password="another-long-pass").status_code == 401
        response = _login(auth_env.client, password="another-long-pass")
        assert response.status_code == 429
        assert response.json()["message"] == "rate_limited"


# ---------------------------------------------------------------------------
# POST /auth/refresh — rotation, grace window, replay detection
# ---------------------------------------------------------------------------


class TestRefresh:
    def test_rotation_issues_new_pair_and_kills_old_access(self, auth_env):
        access, refresh = _register_and_tokens(auth_env.client)
        assert auth_env.client.get("/auth/me", headers=_bearer(access)).status_code == 200

        auth_env.clock.advance(60)
        response = auth_env.client.post("/auth/refresh", json={"refresh_token": refresh})

        assert response.status_code == 200
        body = response.json()
        assert body["rotation"] == 1
        assert body["access_token"] != access
        assert body["refresh_token"] != refresh
        assert body["token_type"] == "Bearer"
        # Absolute refresh expiry is fixed at family creation.
        assert body["refresh_expires_at"] == (EPOCH + REFRESH_TOKEN_TTL).isoformat()
        assert body["access_expires_at"] == (
            EPOCH + timedelta(seconds=60) + ACCESS_TOKEN_TTL
        ).isoformat()
        assert response.headers.get("Cache-Control") == "no-store"

        # Old access is dead; new access works.
        old = auth_env.client.get("/auth/me", headers=_bearer(access))
        assert old.status_code == 401
        assert auth_env.client.get("/auth/me", headers=_bearer(body["access_token"])).status_code == 200

    def test_refresh_after_access_expired_recovers_session(self, auth_env):
        access, refresh = _register_and_tokens(auth_env.client)
        auth_env.clock.advance(ACCESS_TOKEN_TTL.total_seconds() + 60)

        expired = auth_env.client.get("/auth/me", headers=_bearer(access))
        assert expired.status_code == 401
        assert expired.json()["message"] == "access_token_expired"

        response = auth_env.client.post("/auth/refresh", json={"refresh_token": refresh})
        assert response.status_code == 200

        new_access = response.json()["access_token"]
        assert auth_env.client.get("/auth/me", headers=_bearer(new_access)).status_code == 200

    def test_previous_refresh_recovers_within_grace_window(self, auth_env):
        _, refresh = _register_and_tokens(auth_env.client)
        first = auth_env.client.post("/auth/refresh", json={"refresh_token": refresh})
        assert first.status_code == 200

        auth_env.clock.advance(PREVIOUS_REFRESH_GRACE.total_seconds() - 2)
        # Lost-response recovery with the same (now previous) refresh token.
        second = auth_env.client.post("/auth/refresh", json={"refresh_token": refresh})
        assert second.status_code == 200
        assert second.json()["rotation"] == 2

    def test_replay_after_grace_revokes_family_with_stable_code(self, auth_env):
        _, refresh = _register_and_tokens(auth_env.client)
        rotated = auth_env.client.post("/auth/refresh", json={"refresh_token": refresh})
        assert rotated.status_code == 200
        current = rotated.json()

        auth_env.clock.advance(PREVIOUS_REFRESH_GRACE.total_seconds() + 1)
        replay = auth_env.client.post("/auth/refresh", json={"refresh_token": refresh})
        assert replay.status_code == 401
        assert replay.json()["message"] == "refresh_token_reused"

        # The whole family is revoked: the legitimately rotated tokens die too.
        dead_refresh = auth_env.client.post(
            "/auth/refresh", json={"refresh_token": current["refresh_token"]}
        )
        assert dead_refresh.status_code == 401
        assert dead_refresh.json()["message"] == "refresh_token_revoked"
        dead_access = auth_env.client.get(
            "/auth/me", headers=_bearer(current["access_token"])
        )
        assert dead_access.status_code == 401
        assert dead_access.json()["message"] == "session_revoked"

    @pytest.mark.parametrize("bad_token", ["garbage", "krlk_" + "a" * 64, "wca_" + "a" * 32 + "." + "b" * 64])
    def test_invalid_refresh_token_stable_401(self, auth_env, bad_token):
        response = auth_env.client.post("/auth/refresh", json={"refresh_token": bad_token})
        assert response.status_code == 401
        assert response.json()["message"] == "refresh_token_invalid"
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    def test_unknown_refresh_token_401(self, auth_env):
        tokens = WebTokenService(clock=auth_env.clock)
        response = auth_env.client.post(
            "/auth/refresh", json={"refresh_token": tokens.generate_refresh_token()}
        )
        assert response.status_code == 401
        assert response.json()["message"] == "refresh_token_invalid"

    def test_refresh_absolute_expiry_requires_relogin(self, auth_env):
        _, refresh = _register_and_tokens(auth_env.client)
        auth_env.clock.advance(REFRESH_TOKEN_TTL.total_seconds() + 1)
        response = auth_env.client.post("/auth/refresh", json={"refresh_token": refresh})
        assert response.status_code == 401
        assert response.json()["message"] == "refresh_token_expired"

    def test_refresh_rate_limited_429(self, auth_env):
        _, refresh = _register_and_tokens(auth_env.client)
        statuses = []
        for _ in range(31):
            statuses.append(
                auth_env.client.post("/auth/refresh", json={"refresh_token": refresh})
                .status_code
            )
        assert statuses[-1] == 429
        assert statuses[0] == 200
        assert 401 in statuses  # replays after rotation resolve as reuse/revoked


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------


class TestLogout:
    def test_valid_access_204_and_session_dead(self, auth_env):
        access, refresh = _register_and_tokens(auth_env.client)

        response = auth_env.client.post("/auth/logout", headers=_bearer(access))
        assert response.status_code == 204

        assert auth_env.client.get("/auth/me", headers=_bearer(access)).status_code == 401
        refresh_attempt = auth_env.client.post(
            "/auth/refresh", json={"refresh_token": refresh}
        )
        assert refresh_attempt.status_code == 401
        assert refresh_attempt.json()["message"] == "refresh_token_revoked"

    def test_logout_is_idempotent_204(self, auth_env):
        access, _ = _register_and_tokens(auth_env.client)
        assert auth_env.client.post("/auth/logout", headers=_bearer(access)).status_code == 204
        assert auth_env.client.post("/auth/logout", headers=_bearer(access)).status_code == 204

    def test_expired_access_with_refresh_body_204(self, auth_env):
        access, refresh = _register_and_tokens(auth_env.client)
        auth_env.clock.advance(ACCESS_TOKEN_TTL.total_seconds() + 60)

        bare = auth_env.client.post("/auth/logout", headers=_bearer(access))
        assert bare.status_code == 401
        assert bare.json()["message"] == "access_token_expired"

        with_body = auth_env.client.post(
            "/auth/logout", headers=_bearer(access), json={"refresh_token": refresh}
        )
        assert with_body.status_code == 204

    def test_refresh_body_alone_revokes_family(self, auth_env):
        access, refresh = _register_and_tokens(auth_env.client)
        response = auth_env.client.post("/auth/logout", json={"refresh_token": refresh})
        assert response.status_code == 204
        assert auth_env.client.get("/auth/me", headers=_bearer(access)).status_code == 401

    def test_no_credentials_401(self, auth_env):
        response = auth_env.client.post("/auth/logout")
        assert response.status_code == 401
        assert response.json()["message"] == "access_token_invalid"

    def test_unknown_access_401(self, auth_env):
        tokens = WebTokenService(clock=auth_env.clock)
        response = auth_env.client.post(
            "/auth/logout", headers=_bearer(tokens.generate_access_token())
        )
        assert response.status_code == 401
        assert response.json()["message"] == "access_token_invalid"


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------


class TestMe:
    def test_returns_user_and_cube_summary(self, auth_env):
        access, _ = _register_and_tokens(auth_env.client)

        response = auth_env.client.get("/auth/me", headers=_bearer(access))

        assert response.status_code == 200
        body = response.json()
        assert body["user"]["user_name"] == "alice"
        assert body["user"]["role"] == "USER"
        assert [cube["cube_name"] for cube in body["cubes"]] == ["alice"]
        assert "password" not in response.text.lower() or "password" not in body["user"]

    def test_missing_bearer_401_with_www_authenticate(self, auth_env):
        response = auth_env.client.get("/auth/me")
        assert response.status_code == 401
        assert response.json()["message"] == "access_token_invalid"
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    def test_refresh_token_cannot_access_me(self, auth_env):
        _, refresh = _register_and_tokens(auth_env.client)
        response = auth_env.client.get("/auth/me", headers=_bearer(refresh))
        assert response.status_code == 401
        assert response.json()["message"] == "access_token_invalid"

    def test_api_key_format_rejected(self, auth_env):
        response = auth_env.client.get("/auth/me", headers=_bearer("krlk_" + "a" * 64))
        assert response.status_code == 401
        assert response.json()["message"] == "access_token_invalid"

    def test_expired_access_401(self, auth_env):
        access, _ = _register_and_tokens(auth_env.client)
        auth_env.clock.advance(ACCESS_TOKEN_TTL.total_seconds() + 1)
        response = auth_env.client.get("/auth/me", headers=_bearer(access))
        assert response.status_code == 401
        assert response.json()["message"] == "access_token_expired"

    def test_deactivated_user_session_is_rejected(self, auth_env):
        access, _ = _register_and_tokens(auth_env.client)
        user = auth_env.user_manager.get_user_by_name("alice")
        auth_env.user_manager.delete_user(user.user_id)
        response = auth_env.client.get("/auth/me", headers=_bearer(access))
        assert response.status_code == 401
        assert response.json()["message"] == "session_revoked"


# ---------------------------------------------------------------------------
# server_api mounting contract
# ---------------------------------------------------------------------------

MOCK_COMPONENTS_KEYS = (
    "graph_db",
    "mem_reader",
    "llm",
    "embedder",
    "reranker",
    "internet_retriever",
    "memory_manager",
    "default_cube_config",
    "mos_server",
    "mem_scheduler",
    "feedback_server",
    "naive_mem_cube",
    "searcher",
    "api_module",
    "chat_llms",
    "redis_client",
    "deepsearch_agent",
)


def _mock_components() -> dict:
    components = {key: Mock() for key in MOCK_COMPONENTS_KEYS}
    components.update(
        {
            "vector_db": None,
            "pref_extractor": None,
            "pref_adder": None,
            "pref_retriever": None,
            "pref_mem": None,
            "online_bot": None,
        }
    )
    return components


def _fresh_import_server_api(tmp_path, auth_enabled: str):
    env = {
        "AUTH_ENABLED": auth_enabled,
        "RATE_LIMIT_ENABLED": "true",
        "CORS_ORIGINS": "",
        "FILE_LOCAL_PATH": str(tmp_path),
    }
    with (
        patch.dict(os.environ, env),
        patch("memos.api.handlers.init_server", return_value=_mock_components()),
    ):
        sys.modules.pop("memos.api.server_api", None)
        return importlib.import_module("memos.api.server_api")


class TestServerApiMount:
    def test_auth_routes_mounted_when_auth_enabled(self, tmp_path):
        module = _fresh_import_server_api(tmp_path, "true")
        spec = module.app.openapi()
        for path in (
            "/auth/register",
            "/auth/login",
            "/auth/logout",
            "/auth/refresh",
            "/auth/me",
        ):
            assert path in spec["paths"], f"{path} missing from server_api"

    def test_auth_routes_404_when_auth_disabled(self, tmp_path, monkeypatch):
        from memos.api.middleware import auth as auth_module

        # Patch the module attribute (not object identity) so the conditional
        # auth mount re-evaluates; reload/pop would break other test modules'
        # cached from-imports of verify_api_key.
        monkeypatch.setattr(auth_module, "AUTH_ENABLED", False)
        module = _fresh_import_server_api(tmp_path, "false")
        client = TestClient(module.app)
        assert client.post("/auth/login", json={"user_name": "a", "password": "b"}).status_code == 404
        assert client.post("/auth/refresh", json={"refresh_token": "x"}).status_code == 404
        assert client.get("/auth/me").status_code == 404
        # Business health stays public regardless.
        assert client.get("/health").status_code == 200
