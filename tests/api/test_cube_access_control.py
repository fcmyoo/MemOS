"""
CubeAccessControl failure-mode tests (P0-2, plan section 3.2).

Uses a throwaway SQLite database via UserManager; no PostgreSQL involved.
Auth contexts are constructed directly instead of going through verify_api_key.
"""

import pytest

from fastapi import HTTPException

from memos.api.access_control import FORBIDDEN_DETAIL, CubeAccessControl
from memos.mem_user.user_manager import Cube, User, UserManager


@pytest.fixture()
def user_manager(tmp_path):
    """UserManager backed by a throwaway SQLite database."""
    return UserManager(db_path=str(tmp_path / "memos_users.db"))


@pytest.fixture()
def seeded(user_manager):
    """alice owns a cube; bob is an active user without any association."""
    alice_id = user_manager.create_user("alice", user_id="alice-id")
    bob_id = user_manager.create_user("bob", user_id="bob-id")
    cube_id = user_manager.create_cube(
        "alice-cube", owner_id=alice_id, cube_id="alice-cube"
    )
    return {
        "manager": user_manager,
        "access_control": CubeAccessControl(user_manager),
        "alice_id": alice_id,
        "bob_id": bob_id,
        "cube_id": cube_id,
    }


def regular_auth(user_name: str, scopes: list[str] | None = None) -> dict:
    """Auth context of a regular API key (no master/internal/bypass flags)."""
    return {
        "user_name": user_name,
        "scopes": scopes or ["read", "write"],
        "is_master_key": False,
        "api_key_id": "key-1",
    }


def assert_uniform_403(exc_info) -> None:
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == FORBIDDEN_DETAIL
    assert FORBIDDEN_DETAIL == "Insufficient cube access"


def spy_validate(manager) -> list[tuple[str, str]]:
    """Record validate_user_cube_access calls while keeping real behavior."""
    calls: list[tuple[str, str]] = []
    real = manager.validate_user_cube_access

    def _spy(user_id, cube_id):
        calls.append((user_id, cube_id))
        return real(user_id, cube_id)

    manager.validate_user_cube_access = _spy
    return calls


def forbid_sqlite(manager) -> None:
    """Fail loudly if bypassed/privileged paths still hit the SQLite manager."""

    def _deny(*args, **kwargs):
        raise AssertionError("SQLite must not be queried in bypass/privileged mode")

    manager.get_user_by_name = _deny
    manager.validate_user_cube_access = _deny


def restore_sqlite(manager) -> None:
    """Drop the forbid_sqlite instance overrides, restoring bound methods."""
    del manager.get_user_by_name
    del manager.validate_user_cube_access


def deactivate_user(manager, user_id: str) -> None:
    session = manager.SessionLocal()
    try:
        user = session.get(User, user_id)
        user.is_active = False
        session.commit()
    finally:
        session.close()


def deactivate_cube(manager, cube_id: str) -> None:
    session = manager.SessionLocal()
    try:
        cube = session.get(Cube, cube_id)
        cube.is_active = False
        session.commit()
    finally:
        session.close()


# 1. owner: cube owner passes require_cube_access.
def test_owner_access_allowed(seeded):
    access_control = seeded["access_control"]
    auth = regular_auth("alice")

    actor = access_control.resolve_actor(auth)

    assert actor == seeded["alice_id"]
    access_control.require_cube_access(auth, actor, [seeded["cube_id"]])  # no raise


# 2. association: user granted via add_user_to_cube passes.
def test_association_access_allowed(seeded):
    access_control, manager = seeded["access_control"], seeded["manager"]
    assert manager.add_user_to_cube(seeded["bob_id"], seeded["cube_id"]) is True
    auth = regular_auth("bob")

    actor = access_control.resolve_actor(auth)

    assert actor == seeded["bob_id"]
    access_control.require_cube_access(auth, actor, [seeded["cube_id"]])  # no raise


# 3. other user: active user without association gets the fixed 403/detail.
def test_unassociated_active_user_gets_uniform_403(seeded):
    access_control = seeded["access_control"]
    auth = regular_auth("bob")
    actor = access_control.resolve_actor(auth)

    with pytest.raises(HTTPException) as exc_info:
        access_control.require_cube_access(auth, actor, [seeded["cube_id"]])

    assert_uniform_403(exc_info)


# 4. missing/inactive cube: indistinguishable from plain unauthorized (anti-enumeration).
@pytest.mark.parametrize("cube_kind", ["missing", "inactive"])
def test_missing_or_inactive_cube_is_indistinguishable(seeded, cube_kind):
    access_control = seeded["access_control"]
    auth = regular_auth("bob")
    actor = access_control.resolve_actor(auth)

    if cube_kind == "inactive":
        deactivate_cube(seeded["manager"], seeded["cube_id"])
        cube_id = seeded["cube_id"]
    else:
        cube_id = "no-such-cube"

    with pytest.raises(HTTPException) as exc_info:
        access_control.require_cube_access(auth, actor, [cube_id])

    assert_uniform_403(exc_info)


# 5. missing/inactive SQLite user mapping: fixed 403, never "user not found".
@pytest.mark.parametrize("user_kind", ["missing", "inactive"])
def test_missing_or_inactive_user_mapping_is_uniform_403(seeded, user_kind):
    access_control = seeded["access_control"]

    if user_kind == "missing":
        auth = regular_auth("ghost")
    else:
        deactivate_user(seeded["manager"], seeded["alice_id"])
        auth = regular_auth("alice")

    with pytest.raises(HTTPException) as exc_info:
        access_control.resolve_actor(auth)

    assert_uniform_403(exc_info)


# 6. forged claim: authenticated alice claiming bob's user id is rejected.
def test_forged_claim_is_rejected(seeded):
    access_control = seeded["access_control"]

    with pytest.raises(HTTPException) as exc_info:
        access_control.resolve_actor(regular_auth("alice"), claimed_user_id=seeded["bob_id"])

    assert_uniform_403(exc_info)


# 7. multi-Cube all-or-nothing: one unauthorized cube fails the whole request.
def test_multi_cube_is_all_or_nothing(seeded):
    access_control, manager = seeded["access_control"], seeded["manager"]
    other_cube = manager.create_cube(
        "alice-cube-2", owner_id=seeded["alice_id"], cube_id="alice-cube-2"
    )
    assert manager.add_user_to_cube(seeded["bob_id"], seeded["cube_id"]) is True
    auth = regular_auth("bob")
    actor = access_control.resolve_actor(auth)

    with pytest.raises(HTTPException) as exc_info:
        access_control.require_cube_access(auth, actor, [seeded["cube_id"], other_cube])

    assert_uniform_403(exc_info)


# 8. duplicate Cube ids are deduplicated: each cube validated at most once.
def test_duplicate_cubes_validated_once(seeded):
    access_control, manager = seeded["access_control"], seeded["manager"]
    second_cube = manager.create_cube(
        "alice-cube-2", owner_id=seeded["alice_id"], cube_id="alice-cube-2"
    )
    calls = spy_validate(manager)
    auth = regular_auth("alice")
    actor = access_control.resolve_actor(auth)

    access_control.require_cube_access(
        auth, actor, [seeded["cube_id"], seeded["cube_id"], second_cube, second_cube]
    )

    assert calls == [
        (seeded["alice_id"], seeded["cube_id"]),
        (seeded["alice_id"], second_cube),
    ]


# 9. auth_bypassed=True: claimed user wins, SQLite/ACL skipped entirely.
def test_auth_bypassed_prefers_claimed_user_and_skips_checks(seeded):
    access_control, manager = seeded["access_control"], seeded["manager"]
    forbid_sqlite(manager)
    auth = {
        "user_name": "default",
        "scopes": ["all"],
        "is_master_key": False,
        "auth_bypassed": True,
    }

    assert access_control.resolve_actor(auth, claimed_user_id="alice-id") == "alice-id"
    assert access_control.resolve_actor(auth) == "default"
    # Any cube list passes without validation in bypass mode.
    access_control.require_cube_access(auth, "whoever", ["any-cube", ""])


# 10. master/internal are privileged; plain scopes=["all"] must NOT bypass.
def test_master_and_internal_bypass_but_scope_all_does_not(seeded):
    access_control, manager = seeded["access_control"], seeded["manager"]
    bob_cube = manager.create_cube("bob-cube", owner_id=seeded["bob_id"], cube_id="bob-cube")

    forbid_sqlite(manager)
    master_auth = {"user_name": "admin", "scopes": ["all"], "is_master_key": True}
    internal_auth = {
        "user_name": "internal",
        "scopes": ["all"],
        "is_master_key": False,
        "is_internal": True,
    }

    assert access_control.resolve_actor(master_auth) == "admin"
    assert access_control.resolve_actor(master_auth, claimed_user_id="alice-id") == "alice-id"
    assert access_control.resolve_actor(internal_auth) == "internal"
    access_control.require_cube_access(master_auth, "admin", [bob_cube])  # no raise
    access_control.require_cube_access(internal_auth, "internal", [bob_cube])  # no raise

    # Negative control: a regular key whose scopes happen to be ["all"] is not
    # privileged and still goes through the SQLite ACL.
    restore_sqlite(manager)
    scopes_all_auth = regular_auth("alice", scopes=["all"])
    actor = access_control.resolve_actor(scopes_all_auth)

    with pytest.raises(HTTPException) as exc_info:
        access_control.require_cube_access(scopes_all_auth, actor, [bob_cube])

    assert_uniform_403(exc_info)
