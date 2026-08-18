"""PostgreSQL contract tests for ``PostgresUserManager``.

These tests lock the SQLite ``UserManager`` contract onto the PostgreSQL
backend, per docs/plans/user-manager-postgres-migration.md (T1 test matrix):

- initialization / schema bootstrap / root seeding;
- user read paths (create/get/by-name/validate/list);
- password and admin paths (set/count/search/update);
- cube query paths (owned/default/active/get/user-cubes/access);
- cube write paths (create/add/remove/set — ``set_user_cubes`` is one
  transaction and owner membership is permanent);
- soft delete + close;
- PostgreSQL catalog assertions (``password_hash`` nullable, ``role``'s
  underlying type is ``user_role``, the enum carries exactly the four
  uppercase labels, and the three business tables live in the test schema).

``PostgresUserManager`` does not exist yet, so this module fails to import
until T2 lands — the intended TDD RED signal.
"""

import uuid

import psycopg2
import pytest

from memos.mem_user.postgres_user_manager import PostgresUserManager
from memos.mem_user.user_manager import UserRole


# A representative Argon2id PHC string. The storage layer must persist the
# caller-supplied hash verbatim — no re-hashing, no truncation.
ARGON2_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$c29tZWhhc2g"

_SUMMARY_FIELDS = {
    "user_id",
    "user_name",
    "role",
    "is_active",
    "created_at",
    "default_cube_id",
}


def _pg_conn(env):
    """Open a raw psycopg2 connection to the test database (no search_path)."""
    return psycopg2.connect(
        host=env.url.host,
        port=env.url.port,
        user=env.url.username,
        password=env.url.password,
        dbname=env.url.database,
    )


def _pg_rows(env, query, params=()):
    conn = _pg_conn(env)
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()
    finally:
        conn.close()


@pytest.fixture
def manager(postgres_test_schema):
    mgr = PostgresUserManager(
        database_url=postgres_test_schema.url,
        schema=postgres_test_schema.schema,
    )
    postgres_test_schema.register(mgr.engine)
    yield mgr
    mgr.close()


class TestInitialization:
    def test_root_user_seeded_with_nullable_password_hash(self, manager):
        root = manager.get_user("root")
        assert root is not None
        assert root.user_name == "root"
        assert root.role.value == "ROOT"
        assert root.is_active is True
        assert root.password_hash is None

    def test_reinit_does_not_duplicate_root_or_break_tables(
        self, manager, postgres_test_schema
    ):
        manager.create_user("kept", UserRole.USER)

        second = PostgresUserManager(
            database_url=postgres_test_schema.url,
            schema=postgres_test_schema.schema,
        )
        postgres_test_schema.register(second.engine)
        try:
            # Exactly one root plus the pre-existing user: no second root.
            assert second.count_users() == 2
            assert second.get_user_by_name("kept") is not None
            assert second.get_user_by_name("root") is not None
        finally:
            second.close()

    def test_migrate_schema_is_idempotent(self, manager):
        manager._migrate_schema()
        manager._migrate_schema()  # re-running must not raise or drop tables

    def test_get_session_returns_usable_session(self, manager):
        session = manager._get_session()
        try:
            assert session is not None
        finally:
            session.close()


class TestUserRead:
    def test_create_user_returns_uuid_and_persists(self, manager):
        user_id = manager.create_user("test_user", UserRole.USER)
        uuid.UUID(user_id)  # generated ids are UUIDs
        user = manager.get_user(user_id)
        assert user.user_name == "test_user"
        assert user.role.value == "USER"
        assert user.is_active is True

    def test_create_user_with_custom_id(self, manager):
        user_id = manager.create_user("custom", UserRole.ADMIN, "custom-id")
        assert user_id == "custom-id"
        assert manager.get_user("custom-id").role.value == "ADMIN"

    def test_create_duplicate_user_returns_existing_id(self, manager):
        first = manager.create_user("dup", UserRole.USER)
        second = manager.create_user("dup", UserRole.ADMIN)
        assert first == second
        assert manager.get_user(first).role.value == "USER"  # original preserved

    def test_get_user_by_name(self, manager):
        user_id = manager.create_user("named", UserRole.USER)
        assert manager.get_user_by_name("named").user_id == user_id
        assert manager.get_user_by_name("ghost") is None

    def test_validate_user(self, manager):
        user_id = manager.create_user("valid", UserRole.USER)
        assert manager.validate_user(user_id) is True
        assert manager.validate_user("ghost") is False
        manager.delete_user(user_id)
        assert manager.validate_user(user_id) is False

    def test_list_users_only_active(self, manager):
        manager.create_user("u1", UserRole.USER)
        manager.create_user("u2", UserRole.ADMIN)
        guest = manager.create_user("u3", UserRole.GUEST)
        assert {u.user_name for u in manager.list_users()} == {"root", "u1", "u2", "u3"}
        manager.delete_user(guest)
        assert {u.user_name for u in manager.list_users()} == {"root", "u1", "u2"}


class TestPasswordAndAdmin:
    def test_set_user_password_stores_hash_verbatim(self, manager):
        user_id = manager.create_user("webuser", UserRole.USER)
        assert manager.set_user_password(user_id, ARGON2_HASH) is True
        assert manager.get_user(user_id).password_hash == ARGON2_HASH

    def test_set_user_password_missing_user_returns_false(self, manager):
        assert manager.set_user_password("ghost", ARGON2_HASH) is False

    def test_count_users_with_filters(self, manager):
        manager.create_user("a", UserRole.USER)
        manager.create_user("b", UserRole.ADMIN)
        c_id = manager.create_user("c", UserRole.USER)
        assert manager.count_users() == 4
        assert manager.count_users(role=UserRole.USER) == 2
        assert manager.count_users(is_active=True) == 4
        manager.delete_user(c_id)
        # count_users() with no filters counts ALL rows (SQLite semantics:
        # soft-deleted users remain), only is_active=... narrows it.
        assert manager.count_users() == 4
        assert manager.count_users(is_active=True) == 3
        assert manager.count_users(is_active=False) == 1

    def test_search_users_pagination_and_role_filter(self, manager):
        for name in ("p1", "p2", "p3"):
            manager.create_user(name, UserRole.USER)
        manager.create_user("admin", UserRole.ADMIN)

        total, rows = manager.search_users(role=UserRole.USER, offset=0, limit=2)
        assert total == 3
        assert [r["user_name"] for r in rows] == ["p1", "p2"]

        _, rows2 = manager.search_users(role=UserRole.USER, offset=2, limit=2)
        assert [r["user_name"] for r in rows2] == ["p3"]

    def test_search_users_row_contract_and_default_cube(self, manager):
        user_id = manager.create_user("withcube", UserRole.USER)
        cube_id = manager.create_cube("team", owner_id=user_id)

        _, rows = manager.search_users(limit=100)
        row = next(r for r in rows if r["user_name"] == "withcube")
        assert set(row.keys()) == _SUMMARY_FIELDS
        assert row["role"] == "USER"
        assert row["default_cube_id"] == cube_id

    def test_update_user_role_and_active(self, manager):
        user_id = manager.create_user("target", UserRole.USER)
        assert manager.update_user(user_id, role=UserRole.ADMIN, is_active=False) is True
        user = manager.get_user(user_id)
        assert user.role.value == "ADMIN"
        assert user.is_active is False
        assert manager.update_user("ghost", role=UserRole.USER) is False


class TestCubeQuery:
    def test_get_owned_cube_ids_and_default_cube(self, manager):
        owner = manager.create_user("owner", UserRole.USER)
        c1 = manager.create_cube("c1", owner)
        c2 = manager.create_cube("c2", owner)
        assert manager.get_owned_cube_ids(owner) == [c1, c2]
        assert manager.get_default_cube_id(owner) == c1
        assert manager.get_default_cube_id("ghost") is None

    def test_get_cube(self, manager):
        owner = manager.create_user("owner", UserRole.USER)
        cid = manager.create_cube("c", owner)
        cube = manager.get_cube(cid)
        assert cube.cube_name == "c"
        assert cube.owner_id == owner
        assert manager.get_cube("ghost") is None

    def test_get_user_cubes_owned_and_shared(self, manager):
        owner = manager.create_user("owner", UserRole.USER)
        other = manager.create_user("other", UserRole.USER)
        c1 = manager.create_cube("c1", owner)
        c2 = manager.create_cube("c2", owner)
        c3 = manager.create_cube("c3", other)
        manager.add_user_to_cube(other, c1)

        assert {c.cube_id for c in manager.get_user_cubes(other)} == {c1, c3}
        assert {c.cube_id for c in manager.get_user_cubes(owner)} == {c1, c2}

    def test_validate_user_cube_access(self, manager):
        owner = manager.create_user("owner", UserRole.USER)
        other = manager.create_user("other", UserRole.USER)
        cid = manager.create_cube("c", owner)

        assert manager.validate_user_cube_access(owner, cid) is True
        assert manager.validate_user_cube_access(other, cid) is False
        manager.add_user_to_cube(other, cid)
        assert manager.validate_user_cube_access(other, cid) is True
        assert manager.validate_user_cube_access("ghost", cid) is False
        assert manager.validate_user_cube_access(owner, "ghost") is False

    def test_list_active_cubes_excludes_inactive(self, manager):
        owner = manager.create_user("owner", UserRole.USER)
        c1 = manager.create_cube("c1", owner)
        c2 = manager.create_cube("c2", owner)
        manager.delete_cube(c2)

        ids = {c["cube_id"] for c in manager.list_active_cubes()}
        assert c1 in ids
        assert c2 not in ids


class TestCubeWrite:
    def test_create_cube_invalid_owner_raises(self, manager):
        with pytest.raises(ValueError, match="does not exist"):
            manager.create_cube("c", "ghost")

    def test_create_cube_with_path_and_custom_id(self, manager):
        owner = manager.create_user("owner", UserRole.USER)
        cid = manager.create_cube("c", owner, cube_path="/p", cube_id="custom-cube")
        assert cid == "custom-cube"
        cube = manager.get_cube("custom-cube")
        assert cube.cube_path == "/p"
        assert cube.owner_id == owner

    def test_add_and_remove_user_from_cube(self, manager):
        owner = manager.create_user("owner", UserRole.USER)
        other = manager.create_user("other", UserRole.USER)
        cid = manager.create_cube("c", owner)

        assert manager.add_user_to_cube(other, cid) is True
        assert manager.add_user_to_cube(other, cid) is True  # idempotent
        assert manager.validate_user_cube_access(other, cid) is True
        assert manager.remove_user_from_cube(other, cid) is True
        assert manager.validate_user_cube_access(other, cid) is False
        # owner membership cannot be removed
        assert manager.remove_user_from_cube(owner, cid) is False
        assert manager.validate_user_cube_access(owner, cid) is True
        # unknown user / cube fail cleanly
        assert manager.add_user_to_cube("ghost", cid) is False
        assert manager.remove_user_from_cube(other, "ghost") is False

    def test_set_user_cubes_replaces_membership_and_keeps_owner(self, manager):
        owner = manager.create_user("owner", UserRole.USER)
        other = manager.create_user("other", UserRole.USER)
        c1 = manager.create_cube("c1", owner)
        c2 = manager.create_cube("c2", owner)
        c3 = manager.create_cube("c3", other)
        manager.add_user_to_cube(other, c1)
        manager.add_user_to_cube(other, c2)

        assert manager.set_user_cubes(other, [c1, c3]) is True
        assert {c.cube_id for c in manager.get_user_cubes(other)} == {c1, c3}

        # Empty set keeps only the owner cube (c3); c1 is a plain membership.
        assert manager.set_user_cubes(other, []) is True
        assert {c.cube_id for c in manager.get_user_cubes(other)} == {c3}

    def test_set_user_cubes_unknown_user_returns_false(self, manager):
        assert manager.set_user_cubes("ghost", []) is False


class TestDeleteAndClose:
    def test_delete_user_is_soft(self, manager):
        uid = manager.create_user("d", UserRole.USER)
        assert manager.delete_user(uid) is True
        user = manager.get_user(uid)
        assert user is not None
        assert user.is_active is False
        assert manager.delete_user("ghost") is False

    def test_delete_root_returns_false(self, manager):
        assert manager.delete_user("root") is False
        assert manager.validate_user("root") is True

    def test_delete_cube_is_soft(self, manager):
        owner = manager.create_user("owner", UserRole.USER)
        cid = manager.create_cube("c", owner)
        assert manager.delete_cube(cid) is True
        cube = manager.get_cube(cid)
        assert cube.is_active is False
        assert manager.validate_user_cube_access(owner, cid) is False
        assert manager.delete_cube("ghost") is False

    def test_close_is_idempotent(self, manager):
        manager.close()
        manager.close()  # must not raise


class TestPostgresCatalog:
    def test_password_hash_column_exists_and_is_nullable(self, manager, postgres_test_schema):
        rows = _pg_rows(
            postgres_test_schema,
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'users' "
            "AND column_name = 'password_hash'",
            (postgres_test_schema.schema,),
        )
        assert len(rows) == 1
        assert rows[0][0] == "YES"

    def test_role_column_underlying_type_is_user_role(self, manager, postgres_test_schema):
        rows = _pg_rows(
            postgres_test_schema,
            "SELECT t.typname FROM pg_attribute a "
            "JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_type t ON t.oid = a.atttypid "
            "WHERE n.nspname = %s AND c.relname = 'users' AND a.attname = 'role'",
            (postgres_test_schema.schema,),
        )
        assert len(rows) == 1
        assert rows[0][0] == "user_role"

    def test_user_role_enum_has_exactly_four_uppercase_labels(
        self, manager, postgres_test_schema
    ):
        rows = _pg_rows(
            postgres_test_schema,
            "SELECT e.enumlabel FROM pg_enum e "
            "JOIN pg_type t ON t.oid = e.enumtypid "
            "JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE t.typname = 'user_role' AND n.nspname = %s "
            "ORDER BY e.enumsortorder",
            (postgres_test_schema.schema,),
        )
        assert [r[0] for r in rows] == ["ROOT", "ADMIN", "USER", "GUEST"]

    def test_business_tables_live_in_test_schema(self, manager, postgres_test_schema):
        rows = _pg_rows(
            postgres_test_schema,
            "SELECT tablename FROM pg_tables WHERE schemaname = %s",
            (postgres_test_schema.schema,),
        )
        tables = {r[0] for r in rows}
        assert {"users", "cubes", "user_cube_association"} <= tables
