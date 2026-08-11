"""Tests for the Web console admin surface (phases 5+6).

Covers, per docs/plans/web-console-revamp.md §4.3 and the task contract:

- RBAC matrix: ROOT can CRUD every role; ADMIN cannot create/modify/disable/
  delete ROOT or ADMIN (403); USER/GUEST sessions get a fixed 403 on every
  ``/admin/*`` route (no user enumeration);
- GET /admin/users: pagination plus ``role``/``is_active`` filters, response
  restricted to ``user_id,user_name,role,is_active,created_at,default_cube_id``;
- POST /admin/users: admin-proxied creation (role defaults to USER, optional
  initial password and ``cube_ids``, never returns password material);
- GET/PATCH/DELETE /admin/users/{user_id}: hierarchy constraints, is_active
  toggling, password reset revoking every session of the target, last-ROOT
  protection;
- PUT /admin/users/{user_id}/cubes: atomic replacement of the cube set, owner
  cubes irremovable, invalid/inactive cubes rejected with 422;
- hybrid auth: ``wca_`` session admins AND legacy admin-scope ``krlk_`` keys
  (owner mapped to ROOT/ADMIN) both pass; master key keeps working;
- api_keys.revoke_api_key_for_user: single atomic owner-conditioned UPDATE.

Services are injected (tmp SQLite, FakeClock, fake Argon2 hasher) so no real
time, hashing cost or deployment database is involved.
"""

from __future__ import annotations

import hashlib

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from memos.api.middleware import auth as auth_middleware
from memos.api.routers import admin_router as admin_router_module
from memos.api.routers.auth_router import (
    AuthServices,
    WebAuthHTTPError,
    router as auth_router,
    set_auth_services,
    web_auth_error_handler,
)
from memos.api.utils.api_keys import revoke_api_key_for_user
from memos.api.web_auth import (
    FakeClock,
    SessionService,
    WebPasswordService,
    WebRateLimiter,
    WebSessionStore,
    WebTokenService,
)
from memos.mem_user.user_manager import UserManager, UserRole


EPOCH = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
PASSWORD = "correct-horse-battery"  # >= 12 chars, not on the common list
NEW_PASSWORD = "another-strong-pass-9"

SUMMARY_FIELDS = {
    "user_id",
    "user_name",
    "role",
    "is_active",
    "created_at",
    "default_cube_id",
}


class _FakeArgon2Hasher:
    """Stand-in for argon2.PasswordHasher (optional dependency)."""

    def hash(self, password: str) -> str:
        return f"$argon2id$v=19$fake${hashlib.sha256(password.encode()).hexdigest()}"

    def verify(self, password_hash: str, password: str) -> bool:
        if password_hash != self.hash(password):
            raise ValueError("password does not match")
        return True

    def check_needs_rehash(self, password_hash: str) -> bool:
        return False


@pytest.fixture()
def admin_env(tmp_path, monkeypatch):
    """App mounting both auth and admin routers with injected services.

    Pins the middleware module attributes the real ``verify_api_key`` reads so
    a stray local ``.env`` can never flip the tests into bypass mode.
    """
    monkeypatch.setattr(auth_middleware, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth_middleware, "MASTER_KEY_HASH", None)
    monkeypatch.delenv("REGISTRATION_MODE", raising=False)
    monkeypatch.delenv("WEB_CONSOLE_INVITE_CODE", raising=False)

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
    app.include_router(admin_router_module.router)

    yield SimpleNamespace(
        client=TestClient(app),
        clock=clock,
        services=services,
        user_manager=user_manager,
        store=store,
    )

    set_auth_services(None)
    user_manager.close()


@pytest.fixture()
def fake_key_auth(monkeypatch):
    """Replace admin_router's key verification with a scripted stand-in.

    Simulates the legacy API-key paths (admin-scope krlk_ keys, master key,
    internal service, AUTH_ENABLED=false bypass) without PostgreSQL.
    """

    def install(user_name="boss", scopes=("admin",), is_master_key=False, **extra):
        async def _fake_verify(request, api_key=None):
            ctx = {
                "user_name": user_name,
                "scopes": list(scopes),
                "is_master_key": is_master_key,
            }
            ctx.update(extra)
            return ctx

        monkeypatch.setattr(admin_router_module, "verify_api_key", _fake_verify)

    return install


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _login(client, user_name, password=PASSWORD):
    response = client.post("/auth/login", json={"user_name": user_name, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _register(client, user_name="alice", password=PASSWORD):
    response = client.post("/auth/register", json={"user_name": user_name, "password": password})
    assert response.status_code == 201, response.text
    return response.json()


def _grant_password(um, user_name, password=PASSWORD):
    user = um.get_user_by_name(user_name)
    assert user is not None
    assert um.set_user_password(user.user_id, _FakeArgon2Hasher().hash(password))
    return user


def _root_token(env):
    _grant_password(env.user_manager, "root")
    return _login(env.client, "root")


def _admin_token(env, name="boss"):
    env.user_manager.create_user(name, role=UserRole.ADMIN)
    _grant_password(env.user_manager, name)
    return _login(env.client, name)


def _user_id(env, user_name):
    user = env.user_manager.get_user_by_name(user_name)
    assert user is not None
    return user.user_id


# ---------------------------------------------------------------------------
# RBAC matrix
# ---------------------------------------------------------------------------


class TestRBACMatrix:
    def test_root_can_create_admin_and_user(self, admin_env):
        token = _root_token(admin_env)

        for name, role in (("sub_admin", "ADMIN"), ("pleb", "USER")):
            response = admin_env.client.post(
                "/admin/users",
                json={"user_name": name, "role": role},
                headers=_bearer(token),
            )
            assert response.status_code == 201, response.text
            assert response.json()["user"]["role"] == role

    def test_root_can_patch_and_delete_another_admin(self, admin_env):
        token = _root_token(admin_env)
        created = admin_env.client.post(
            "/admin/users",
            json={"user_name": "sub_admin", "role": "ADMIN"},
            headers=_bearer(token),
        ).json()["user"]

        response = admin_env.client.patch(
            f"/admin/users/{created['user_id']}",
            json={"is_active": False},
            headers=_bearer(token),
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False

        # Re-activate, then delete: DELETE targets active accounts only.
        response = admin_env.client.patch(
            f"/admin/users/{created['user_id']}",
            json={"is_active": True},
            headers=_bearer(token),
        )
        assert response.status_code == 200

        response = admin_env.client.delete(
            f"/admin/users/{created['user_id']}", headers=_bearer(token)
        )
        assert response.status_code == 204

    def test_admin_cannot_create_admin_or_root(self, admin_env):
        token = _admin_token(admin_env)

        for role in ("ADMIN", "ROOT"):
            response = admin_env.client.post(
                "/admin/users",
                json={"user_name": f"escalated_{role.lower()}", "role": role},
                headers=_bearer(token),
            )
            assert response.status_code == 403, response.text

    def test_admin_cannot_patch_root_or_admin(self, admin_env):
        root_token = _root_token(admin_env)
        admin_env.client.post(
            "/admin/users",
            json={"user_name": "peer_admin", "role": "ADMIN"},
            headers=_bearer(root_token),
        )
        token = _admin_token(admin_env)

        for target in ("root", "peer_admin"):
            response = admin_env.client.patch(
                f"/admin/users/{_user_id(admin_env, target)}",
                json={"is_active": False},
                headers=_bearer(token),
            )
            assert response.status_code == 403, response.text

    def test_admin_cannot_delete_root_or_admin(self, admin_env):
        root_token = _root_token(admin_env)
        admin_env.client.post(
            "/admin/users",
            json={"user_name": "peer_admin", "role": "ADMIN"},
            headers=_bearer(root_token),
        )
        token = _admin_token(admin_env)

        for target in ("root", "peer_admin"):
            response = admin_env.client.delete(
                f"/admin/users/{_user_id(admin_env, target)}", headers=_bearer(token)
            )
            assert response.status_code == 403, response.text

    def test_admin_cannot_promote_user_to_admin(self, admin_env):
        token = _admin_token(admin_env)
        admin_env.user_manager.create_user("pleb", role=UserRole.USER)

        response = admin_env.client.patch(
            f"/admin/users/{_user_id(admin_env, 'pleb')}",
            json={"role": "ADMIN"},
            headers=_bearer(token),
        )
        assert response.status_code == 403

        response = admin_env.client.patch(
            f"/admin/users/{_user_id(admin_env, 'pleb')}",
            json={"role": "ROOT"},
            headers=_bearer(token),
        )
        assert response.status_code == 403

    def test_admin_can_manage_user_and_guest(self, admin_env):
        token = _admin_token(admin_env)
        admin_env.user_manager.create_user("pleb", role=UserRole.USER)
        admin_env.user_manager.create_user("visitor", role=UserRole.GUEST)

        for name, other_role in (("pleb", "GUEST"), ("visitor", "USER")):
            user_id = _user_id(admin_env, name)
            # Role edits within USER/GUEST are allowed for ADMIN ...
            assert (
                admin_env.client.patch(
                    f"/admin/users/{user_id}",
                    json={"role": other_role},
                    headers=_bearer(token),
                ).status_code
                == 200
            )
            # ... and so is deletion of the still-active account.
            assert (
                admin_env.client.delete(f"/admin/users/{user_id}", headers=_bearer(token)).status_code
                == 204
            )

    ADMIN_ROUTE_SPECS = [
        ("get", "/admin/users", None),
        ("post", "/admin/users", {"user_name": "sneaky"}),
        ("get", "/admin/users/{user_id}", None),
        ("patch", "/admin/users/{user_id}", {"is_active": False}),
        ("delete", "/admin/users/{user_id}", None),
        ("get", "/admin/cubes", None),
        ("put", "/admin/users/{user_id}/cubes", {"cube_ids": []}),
    ]

    @pytest.mark.parametrize("method,path,body", ADMIN_ROUTE_SPECS)
    def test_user_session_gets_fixed_403_on_every_admin_route(self, admin_env, method, path, body):
        _register(admin_env.client, user_name="eve")
        token = _login(admin_env.client, "eve")
        target_id = _user_id(admin_env, "eve")
        url = path.format(user_id=target_id)

        response = admin_env.client.request(
            method, url, json=body, headers=_bearer(token)
        )
        assert response.status_code == 403, response.text

    def test_guest_session_gets_403(self, admin_env):
        admin_env.user_manager.create_user("visitor", role=UserRole.GUEST)
        _grant_password(admin_env.user_manager, "visitor")
        token = _login(admin_env.client, "visitor")

        response = admin_env.client.get("/admin/users", headers=_bearer(token))
        assert response.status_code == 403

    def test_missing_credentials_401(self, admin_env):
        response = admin_env.client.get("/admin/users")
        assert response.status_code == 401

    def test_malformed_token_401(self, admin_env):
        response = admin_env.client.get(
            "/admin/users", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code == 401

    def test_refresh_token_cannot_enter_admin(self, admin_env):
        _register(admin_env.client, user_name="eve")
        body = admin_env.client.post(
            "/auth/login", json={"user_name": "eve", "password": PASSWORD}
        ).json()

        response = admin_env.client.get(
            "/admin/users", headers=_bearer(body["refresh_token"])
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /admin/users
# ---------------------------------------------------------------------------


class TestListUsers:
    def test_response_contains_only_contract_fields(self, admin_env):
        _register(admin_env.client, user_name="alice")
        token = _root_token(admin_env)

        response = admin_env.client.get("/admin/users", headers=_bearer(token))

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == len(body["users"])
        assert body["page"] == 1
        assert body["limit"] == 20
        assert body["users"], "expected at least the seeded root user"
        for item in body["users"]:
            assert set(item.keys()) == SUMMARY_FIELDS
        assert "password" not in response.text
        assert "password_hash" not in response.text

    def test_default_cube_id_present_for_registered_user(self, admin_env):
        registered = _register(admin_env.client, user_name="alice")
        token = _root_token(admin_env)

        body = admin_env.client.get("/admin/users", headers=_bearer(token)).json()
        alice = next(u for u in body["users"] if u["user_name"] == "alice")
        root_row = next(u for u in body["users"] if u["user_name"] == "root")

        assert alice["default_cube_id"] == registered["default_cube"]["cube_id"]
        assert root_row["default_cube_id"] is None

    def test_pagination(self, admin_env):
        for index in range(1, 6):
            admin_env.user_manager.create_user(f"pag{index}", role=UserRole.USER)
        token = _root_token(admin_env)

        page_one = admin_env.client.get(
            "/admin/users?role=USER&page=1&limit=2", headers=_bearer(token)
        ).json()
        assert page_one["total"] == 5
        assert [u["user_name"] for u in page_one["users"]] == ["pag1", "pag2"]

        page_two = admin_env.client.get(
            "/admin/users?role=USER&page=2&limit=2", headers=_bearer(token)
        ).json()
        assert [u["user_name"] for u in page_two["users"]] == ["pag3", "pag4"]

        page_three = admin_env.client.get(
            "/admin/users?role=USER&page=3&limit=2", headers=_bearer(token)
        ).json()
        assert [u["user_name"] for u in page_three["users"]] == ["pag5"]

        beyond = admin_env.client.get(
            "/admin/users?role=USER&page=99&limit=2", headers=_bearer(token)
        ).json()
        assert beyond["users"] == []
        assert beyond["total"] == 5

    def test_filter_by_role(self, admin_env):
        _register(admin_env.client, user_name="alice")
        admin_env.user_manager.create_user("carol", role=UserRole.ADMIN)
        token = _root_token(admin_env)

        admins = admin_env.client.get(
            "/admin/users?role=ADMIN", headers=_bearer(token)
        ).json()
        assert {u["user_name"] for u in admins["users"]} == {"carol"}

        roots = admin_env.client.get("/admin/users?role=ROOT", headers=_bearer(token)).json()
        assert {u["user_name"] for u in roots["users"]} == {"root"}

    def test_filter_by_is_active(self, admin_env):
        admin_env.user_manager.create_user("dave", role=UserRole.USER)
        admin_env.user_manager.delete_user(_user_id(admin_env, "dave"))
        token = _root_token(admin_env)

        inactive = admin_env.client.get(
            "/admin/users?is_active=false", headers=_bearer(token)
        ).json()
        assert {u["user_name"] for u in inactive["users"]} == {"dave"}

        active = admin_env.client.get(
            "/admin/users?is_active=true", headers=_bearer(token)
        ).json()
        assert "dave" not in {u["user_name"] for u in active["users"]}

    def test_invalid_role_filter_422(self, admin_env):
        token = _root_token(admin_env)
        response = admin_env.client.get("/admin/users?role=SUPERUSER", headers=_bearer(token))
        assert response.status_code == 422

    def test_invalid_page_422(self, admin_env):
        token = _root_token(admin_env)
        assert (
            admin_env.client.get("/admin/users?page=0", headers=_bearer(token)).status_code == 422
        )
        assert (
            admin_env.client.get("/admin/users?limit=0", headers=_bearer(token)).status_code == 422
        )
        assert (
            admin_env.client.get("/admin/users?limit=500", headers=_bearer(token)).status_code
            == 422
        )


# ---------------------------------------------------------------------------
# POST /admin/users
# ---------------------------------------------------------------------------


class TestCreateUser:
    def test_defaults_to_role_user_201(self, admin_env):
        token = _root_token(admin_env)

        response = admin_env.client.post(
            "/admin/users", json={"user_name": "newbie"}, headers=_bearer(token)
        )

        assert response.status_code == 201, response.text
        user = response.json()["user"]
        assert user["user_name"] == "newbie"
        assert user["role"] == "USER"
        assert user["is_active"] is True
        assert set(user.keys()) == SUMMARY_FIELDS
        assert user["default_cube_id"] is None
        assert response.json()["cubes"] == []

    def test_with_initial_password_and_cubes(self, admin_env):
        root_id = _user_id(admin_env, "root")
        cube_id = admin_env.user_manager.create_cube("team", owner_id=root_id)
        token = _root_token(admin_env)

        response = admin_env.client.post(
            "/admin/users",
            json={"user_name": "newbie", "password": PASSWORD, "cube_ids": [cube_id]},
            headers=_bearer(token),
        )
        assert response.status_code == 201, response.text
        assert [c["cube_id"] for c in response.json()["cubes"]] == [cube_id]

        # The initial password actually works for Web login.
        assert _login(admin_env.client, "newbie") is not None
        cubes = admin_env.user_manager.get_user_cubes(_user_id(admin_env, "newbie"))
        assert [c.cube_id for c in cubes] == [cube_id]

    def test_response_leaks_no_password_material(self, admin_env):
        token = _root_token(admin_env)
        response = admin_env.client.post(
            "/admin/users",
            json={"user_name": "newbie", "password": PASSWORD},
            headers=_bearer(token),
        )
        assert PASSWORD not in response.text
        assert "password_hash" not in response.text

    def test_duplicate_user_name_409(self, admin_env):
        token = _root_token(admin_env)
        assert (
            admin_env.client.post(
                "/admin/users", json={"user_name": "newbie"}, headers=_bearer(token)
            ).status_code
            == 201
        )
        response = admin_env.client.post(
            "/admin/users", json={"user_name": "newbie"}, headers=_bearer(token)
        )
        assert response.status_code == 409

    def test_weak_password_422(self, admin_env):
        token = _root_token(admin_env)
        response = admin_env.client.post(
            "/admin/users",
            json={"user_name": "newbie", "password": "short"},
            headers=_bearer(token),
        )
        assert response.status_code == 422
        assert admin_env.user_manager.get_user_by_name("newbie") is None

    def test_invalid_role_422(self, admin_env):
        token = _root_token(admin_env)
        response = admin_env.client.post(
            "/admin/users",
            json={"user_name": "newbie", "role": "SUPERUSER"},
            headers=_bearer(token),
        )
        assert response.status_code == 422

    def test_invalid_cube_id_422_and_no_partial_user(self, admin_env):
        token = _root_token(admin_env)
        response = admin_env.client.post(
            "/admin/users",
            json={"user_name": "newbie", "cube_ids": ["does-not-exist"]},
            headers=_bearer(token),
        )
        assert response.status_code == 422
        assert admin_env.user_manager.get_user_by_name("newbie") is None

    def test_admin_can_create_plain_user(self, admin_env):
        token = _admin_token(admin_env)
        response = admin_env.client.post(
            "/admin/users", json={"user_name": "newbie"}, headers=_bearer(token)
        )
        assert response.status_code == 201


# ---------------------------------------------------------------------------
# GET / PATCH / DELETE /admin/users/{user_id}
# ---------------------------------------------------------------------------


class TestGetPatchDeleteUser:
    def test_get_detail_returns_user_and_cubes(self, admin_env):
        root_id = _user_id(admin_env, "root")
        cube_id = admin_env.user_manager.create_cube("team", owner_id=root_id)
        token = _root_token(admin_env)
        created = admin_env.client.post(
            "/admin/users",
            json={"user_name": "newbie", "cube_ids": [cube_id]},
            headers=_bearer(token),
        ).json()["user"]

        response = admin_env.client.get(
            f"/admin/users/{created['user_id']}", headers=_bearer(token)
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["user"]["user_name"] == "newbie"
        assert set(body["user"].keys()) == SUMMARY_FIELDS
        assert [c["cube_id"] for c in body["cubes"]] == [cube_id]

    def test_get_unknown_user_404(self, admin_env):
        token = _root_token(admin_env)
        response = admin_env.client.get("/admin/users/ghost-id", headers=_bearer(token))
        assert response.status_code == 404

    def test_patch_toggle_is_active_revokes_sessions(self, admin_env):
        _register(admin_env.client, user_name="alice")
        alice_id = _user_id(admin_env, "alice")
        alice_token = _login(admin_env.client, "alice")
        token = _root_token(admin_env)

        response = admin_env.client.patch(
            f"/admin/users/{alice_id}", json={"is_active": False}, headers=_bearer(token)
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is False

        # Session revoked: the old access token no longer works.
        assert (
            admin_env.client.get("/auth/me", headers=_bearer(alice_token)).status_code == 401
        )
        # And login is refused while the account is disabled.
        assert (
            admin_env.client.post(
                "/auth/login", json={"user_name": "alice", "password": PASSWORD}
            ).status_code
            == 401
        )

        # Re-activating restores login (a fresh session is required).
        response = admin_env.client.patch(
            f"/admin/users/{alice_id}", json={"is_active": True}, headers=_bearer(token)
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is True
        assert _login(admin_env.client, "alice") is not None

    def test_patch_password_reset_revokes_sessions(self, admin_env):
        _register(admin_env.client, user_name="alice")
        alice_id = _user_id(admin_env, "alice")
        alice_token = _login(admin_env.client, "alice")
        token = _root_token(admin_env)

        response = admin_env.client.patch(
            f"/admin/users/{alice_id}", json={"password": NEW_PASSWORD}, headers=_bearer(token)
        )
        assert response.status_code == 200, response.text
        assert NEW_PASSWORD not in response.text

        # Old session revoked, old password rejected, new password accepted.
        assert (
            admin_env.client.get("/auth/me", headers=_bearer(alice_token)).status_code == 401
        )
        assert (
            admin_env.client.post(
                "/auth/login", json={"user_name": "alice", "password": PASSWORD}
            ).status_code
            == 401
        )
        assert _login(admin_env.client, "alice", password=NEW_PASSWORD) is not None

    def test_patch_rejects_user_name_change(self, admin_env):
        token = _root_token(admin_env)
        response = admin_env.client.patch(
            f"/admin/users/{_user_id(admin_env, 'root')}",
            json={"user_name": "renamed"},
            headers=_bearer(token),
        )
        assert response.status_code == 422

    def test_patch_rejects_weak_password(self, admin_env):
        _register(admin_env.client, user_name="alice")
        token = _root_token(admin_env)
        response = admin_env.client.patch(
            f"/admin/users/{_user_id(admin_env, 'alice')}",
            json={"password": "short"},
            headers=_bearer(token),
        )
        assert response.status_code == 422

    def test_patch_unknown_user_404(self, admin_env):
        token = _root_token(admin_env)
        response = admin_env.client.patch(
            "/admin/users/ghost-id", json={"is_active": False}, headers=_bearer(token)
        )
        assert response.status_code == 404

    def test_patch_empty_body_is_noop_200(self, admin_env):
        _register(admin_env.client, user_name="alice")
        alice_token = _login(admin_env.client, "alice")
        token = _root_token(admin_env)

        response = admin_env.client.patch(
            f"/admin/users/{_user_id(admin_env, 'alice')}", json={}, headers=_bearer(token)
        )
        assert response.status_code == 200
        assert response.json()["is_active"] is True
        # No revocation side effect for an empty patch.
        assert admin_env.client.get("/auth/me", headers=_bearer(alice_token)).status_code == 200

    def test_last_root_cannot_be_deactivated(self, admin_env):
        token = _root_token(admin_env)
        response = admin_env.client.patch(
            f"/admin/users/{_user_id(admin_env, 'root')}",
            json={"is_active": False},
            headers=_bearer(token),
        )
        assert response.status_code == 409

    def test_last_root_cannot_be_demoted(self, admin_env):
        token = _root_token(admin_env)
        response = admin_env.client.patch(
            f"/admin/users/{_user_id(admin_env, 'root')}",
            json={"role": "ADMIN"},
            headers=_bearer(token),
        )
        assert response.status_code == 409

    def test_root_can_demote_second_root(self, admin_env):
        token = _root_token(admin_env)
        second = admin_env.client.post(
            "/admin/users",
            json={"user_name": "co_root", "role": "ROOT"},
            headers=_bearer(token),
        ).json()["user"]

        response = admin_env.client.patch(
            f"/admin/users/{second['user_id']}", json={"role": "USER"}, headers=_bearer(token)
        )
        assert response.status_code == 200
        assert response.json()["role"] == "USER"

    def test_delete_success_204_revokes_sessions(self, admin_env):
        _register(admin_env.client, user_name="alice")
        alice_id = _user_id(admin_env, "alice")
        alice_token = _login(admin_env.client, "alice")
        token = _root_token(admin_env)

        response = admin_env.client.delete(f"/admin/users/{alice_id}", headers=_bearer(token))
        assert response.status_code == 204

        assert admin_env.user_manager.get_user(alice_id).is_active is False
        assert admin_env.client.get("/auth/me", headers=_bearer(alice_token)).status_code == 401
        assert (
            admin_env.client.post(
                "/auth/login", json={"user_name": "alice", "password": PASSWORD}
            ).status_code
            == 401
        )

    def test_delete_root_403(self, admin_env):
        token = _root_token(admin_env)
        response = admin_env.client.delete(
            f"/admin/users/{_user_id(admin_env, 'root')}", headers=_bearer(token)
        )
        assert response.status_code == 403

    def test_delete_unknown_user_404(self, admin_env):
        token = _root_token(admin_env)
        response = admin_env.client.delete("/admin/users/ghost-id", headers=_bearer(token))
        assert response.status_code == 404

    def test_delete_already_inactive_user_409(self, admin_env):
        admin_env.user_manager.create_user("dave", role=UserRole.USER)
        dave_id = _user_id(admin_env, "dave")
        admin_env.user_manager.delete_user(dave_id)
        token = _root_token(admin_env)

        response = admin_env.client.delete(f"/admin/users/{dave_id}", headers=_bearer(token))
        assert response.status_code == 409


# ---------------------------------------------------------------------------
# Cube assignment: GET /admin/cubes + PUT /admin/users/{user_id}/cubes
# ---------------------------------------------------------------------------


class TestCubeAssignment:
    def _setup_bob(self, admin_env):
        """bob owns cube_bob and is a member of cube_t1 / cube_t2."""
        token = _root_token(admin_env)
        created = admin_env.client.post(
            "/admin/users", json={"user_name": "bob"}, headers=_bearer(token)
        ).json()["user"]
        bob_id = created["user_id"]
        root_id = _user_id(admin_env, "root")
        cube_bob = admin_env.user_manager.create_cube("bob", owner_id=bob_id)
        cube_t1 = admin_env.user_manager.create_cube("team-1", owner_id=root_id)
        cube_t2 = admin_env.user_manager.create_cube("team-2", owner_id=root_id)
        admin_env.user_manager.add_user_to_cube(bob_id, cube_t1)
        admin_env.user_manager.add_user_to_cube(bob_id, cube_t2)
        return token, bob_id, {"bob": cube_bob, "t1": cube_t1, "t2": cube_t2}

    def test_put_replaces_membership_atomically(self, admin_env):
        token, bob_id, cubes = self._setup_bob(admin_env)

        response = admin_env.client.put(
            f"/admin/users/{bob_id}/cubes",
            json={"cube_ids": [cubes["bob"], cubes["t1"]]},
            headers=_bearer(token),
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["user_id"] == bob_id
        assert {c["cube_id"] for c in body["cubes"]} == {cubes["bob"], cubes["t1"]}

        remaining = {c.cube_id for c in admin_env.user_manager.get_user_cubes(bob_id)}
        assert remaining == {cubes["bob"], cubes["t1"]}

    def test_owner_cube_cannot_be_removed_422(self, admin_env):
        token, bob_id, cubes = self._setup_bob(admin_env)

        response = admin_env.client.put(
            f"/admin/users/{bob_id}/cubes",
            json={"cube_ids": [cubes["t1"]]},
            headers=_bearer(token),
        )
        assert response.status_code == 422

        # Membership untouched.
        remaining = {c.cube_id for c in admin_env.user_manager.get_user_cubes(bob_id)}
        assert remaining == {cubes["bob"], cubes["t1"], cubes["t2"]}

    def test_invalid_cube_id_422(self, admin_env):
        token, bob_id, cubes = self._setup_bob(admin_env)
        response = admin_env.client.put(
            f"/admin/users/{bob_id}/cubes",
            json={"cube_ids": [cubes["bob"], "does-not-exist"]},
            headers=_bearer(token),
        )
        assert response.status_code == 422

    def test_inactive_cube_422(self, admin_env):
        token, bob_id, cubes = self._setup_bob(admin_env)
        admin_env.user_manager.delete_cube(cubes["t2"])

        response = admin_env.client.put(
            f"/admin/users/{bob_id}/cubes",
            json={"cube_ids": [cubes["bob"], cubes["t2"]]},
            headers=_bearer(token),
        )
        assert response.status_code == 422

    def test_put_unknown_user_404(self, admin_env):
        token = _root_token(admin_env)
        response = admin_env.client.put(
            "/admin/users/ghost-id/cubes", json={"cube_ids": []}, headers=_bearer(token)
        )
        assert response.status_code == 404

    def test_empty_set_allowed_when_user_owns_no_cube(self, admin_env):
        root_id = _user_id(admin_env, "root")
        cube_id = admin_env.user_manager.create_cube("team", owner_id=root_id)
        token = _root_token(admin_env)
        created = admin_env.client.post(
            "/admin/users",
            json={"user_name": "nomad", "cube_ids": [cube_id]},
            headers=_bearer(token),
        ).json()["user"]

        response = admin_env.client.put(
            f"/admin/users/{created['user_id']}/cubes",
            json={"cube_ids": []},
            headers=_bearer(token),
        )
        assert response.status_code == 200
        assert response.json()["cubes"] == []
        assert admin_env.user_manager.get_user_cubes(created["user_id"]) == []

    def test_admin_blocked_from_admin_target_cubes(self, admin_env):
        root_token = _root_token(admin_env)
        peer = admin_env.client.post(
            "/admin/users",
            json={"user_name": "peer_admin", "role": "ADMIN"},
            headers=_bearer(root_token),
        ).json()["user"]
        token = _admin_token(admin_env)

        response = admin_env.client.put(
            f"/admin/users/{peer['user_id']}/cubes",
            json={"cube_ids": []},
            headers=_bearer(token),
        )
        assert response.status_code == 403

    def test_admin_can_assign_cubes_for_user(self, admin_env):
        token_root = _root_token(admin_env)
        bob = admin_env.client.post(
            "/admin/users", json={"user_name": "bob"}, headers=_bearer(token_root)
        ).json()["user"]
        cube_id = admin_env.user_manager.create_cube(
            "team", owner_id=_user_id(admin_env, "root")
        )
        token = _admin_token(admin_env)

        response = admin_env.client.put(
            f"/admin/users/{bob['user_id']}/cubes",
            json={"cube_ids": [cube_id]},
            headers=_bearer(token),
        )
        assert response.status_code == 200

    def test_get_admin_cubes_lists_only_active(self, admin_env):
        token, _, cubes = self._setup_bob(admin_env)
        admin_env.user_manager.delete_cube(cubes["t2"])

        response = admin_env.client.get("/admin/cubes", headers=_bearer(token))

        assert response.status_code == 200, response.text
        body = response.json()
        listed = {c["cube_id"] for c in body["cubes"]}
        assert cubes["t2"] not in listed
        assert {cubes["bob"], cubes["t1"]} <= listed
        assert body["total"] == len(body["cubes"])
        for cube in body["cubes"]:
            assert {"cube_id", "cube_name", "owner_id", "created_at"} <= set(cube.keys())


# ---------------------------------------------------------------------------
# Hybrid auth: session admins + legacy keys
# ---------------------------------------------------------------------------


class TestHybridAuth:
    def test_session_admin_and_legacy_key_both_access(self, admin_env, fake_key_auth):
        # Session admin (wca_) ...
        session_token = _admin_token(admin_env)
        response = admin_env.client.get("/admin/users", headers=_bearer(session_token))
        assert response.status_code == 200, response.text

        # ... and a legacy admin-scope key mapped to a ROOT/ADMIN user.
        fake_key_auth(user_name="root", scopes=("admin",))
        response = admin_env.client.get("/admin/users")
        assert response.status_code == 200, response.text

    def test_legacy_key_can_write(self, admin_env, fake_key_auth):
        fake_key_auth(user_name="boss", scopes=("admin",))
        admin_env.user_manager.create_user("boss", role=UserRole.ADMIN)

        response = admin_env.client.post("/admin/users", json={"user_name": "newbie"})
        assert response.status_code == 201, response.text

    def test_admin_scope_key_mapped_to_user_role_403(self, admin_env, fake_key_auth):
        admin_env.user_manager.create_user("eve", role=UserRole.USER)
        fake_key_auth(user_name="eve", scopes=("admin",))

        response = admin_env.client.get("/admin/users")
        assert response.status_code == 403

    def test_admin_scope_key_mapped_to_unknown_user_403(self, admin_env, fake_key_auth):
        fake_key_auth(user_name="ghost", scopes=("admin",))
        response = admin_env.client.get("/admin/users")
        assert response.status_code == 403

    def test_read_scope_key_403(self, admin_env, fake_key_auth):
        admin_env.user_manager.create_user("boss", role=UserRole.ADMIN)
        fake_key_auth(user_name="boss", scopes=("read",))

        response = admin_env.client.get("/admin/users")
        assert response.status_code == 403

    def test_master_key_keeps_access(self, admin_env, fake_key_auth):
        fake_key_auth(user_name="admin", scopes=("all",), is_master_key=True)

        assert admin_env.client.get("/admin/users").status_code == 200
        assert (
            admin_env.client.post("/admin/users", json={"user_name": "newbie"}).status_code == 201
        )

    def test_auth_bypass_keeps_legacy_reach(self, admin_env, fake_key_auth):
        fake_key_auth(user_name="default", scopes=("all",), auth_bypassed=True)
        assert admin_env.client.get("/admin/users").status_code == 200

    def test_internal_service_keeps_legacy_reach(self, admin_env, fake_key_auth):
        fake_key_auth(user_name="internal", scopes=("all",), is_internal=True)
        assert admin_env.client.get("/admin/users").status_code == 200

    def test_session_admin_hierarchy_applies(self, admin_env):
        """A session ADMIN obeys the same hierarchy as a key ADMIN."""
        root_token = _root_token(admin_env)
        admin_env.client.post(
            "/admin/users",
            json={"user_name": "peer_admin", "role": "ADMIN"},
            headers=_bearer(root_token),
        )
        token = _admin_token(admin_env)

        response = admin_env.client.delete(
            f"/admin/users/{_user_id(admin_env, 'peer_admin')}", headers=_bearer(token)
        )
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# revoke_api_key_for_user (owner-safe single atomic UPDATE)
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, rowcount):
        self.rowcount = rowcount
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, rowcount):
        self._rowcount = rowcount
        self.cursors = []
        self.commits = 0

    def cursor(self):
        cursor = _FakeCursor(self._rowcount)
        self.cursors.append(cursor)
        return cursor

    def commit(self):
        self.commits += 1


class TestRevokeApiKeyForUser:
    def test_single_atomic_update_with_owner_condition(self):
        conn = _FakeConn(rowcount=1)

        assert revoke_api_key_for_user(conn, "key-1", "alice") is True

        cursor = conn.cursors[0]
        assert len(cursor.executed) == 1, "must be a single SQL statement"
        sql, params = cursor.executed[0]
        normalized = " ".join(sql.split()).upper()
        assert "UPDATE API_KEYS SET IS_ACTIVE = FALSE" in normalized
        assert "ID = %S" in normalized
        assert "USER_NAME = %S" in normalized
        assert "IS_ACTIVE = TRUE" in normalized
        assert params == ("key-1", "alice")
        assert conn.commits == 1

    def test_returns_false_when_no_row_matches(self):
        conn = _FakeConn(rowcount=0)
        assert revoke_api_key_for_user(conn, "someone-elses-key", "alice") is False
