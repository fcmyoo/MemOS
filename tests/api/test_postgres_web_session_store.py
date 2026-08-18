"""PostgreSQL contract tests for ``WebSessionStore``.

Reuses the public operations and boundaries already covered by
``tests/api/test_web_session.py`` (create / read / access-refresh rotation /
revoke / expiry cleanup) and locks them onto the PostgreSQL backend, per
docs/plans/user-manager-postgres-migration.md T4:

- token hashes only, never plaintext;
- cross-instance visibility through the shared schema (no local file);
- ``close()`` idempotent;
- ``_ensure_schema()`` idempotent.

The PG constructor (``database_url`` / ``schema``) does not exist yet, so these
tests fail with ``TypeError`` until T4 lands — the intended RED signal.
"""

import os
import re
import uuid

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psycopg2
import pytest
from psycopg2 import sql
from sqlalchemy.engine import URL

from memos.api.web_auth import (
    ACCESS_TOKEN_TTL,
    RotateStatus,
    FakeClock,
    WebSessionStore,
    WebTokenService,
)


EPOCH = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)

_SCHEMA_PATTERN = re.compile(r"^[a-z0-9_]+$")
_REQUIRED_ENV = ("HOST", "PORT", "USER", "PASSWORD", "DB")


@pytest.fixture
def postgres_test_schema():
    """Isolated ``memos_test_<uuid>`` schema for the WebSessionStore tests.

    Mirrors ``tests/mem_user/conftest.py``: only ``MEMOS_TEST_POSTGRES_*`` are
    read (missing → skip), and teardown disposes registered engines before
    ``DROP SCHEMA ... CASCADE``.
    """
    missing = [
        name for name in _REQUIRED_ENV if not os.getenv(f"MEMOS_TEST_POSTGRES_{name}")
    ]
    if missing:
        pytest.skip(
            "PostgreSQL integration tests not enabled: missing "
            + ", ".join(f"MEMOS_TEST_POSTGRES_{name}" for name in missing)
        )

    schema = f"memos_test_{uuid.uuid4().hex}"
    assert _SCHEMA_PATTERN.fullmatch(schema)

    url = URL.create(
        drivername="postgresql+psycopg2",
        username=os.environ["MEMOS_TEST_POSTGRES_USER"],
        password=os.environ["MEMOS_TEST_POSTGRES_PASSWORD"],
        host=os.environ["MEMOS_TEST_POSTGRES_HOST"],
        port=int(os.environ["MEMOS_TEST_POSTGRES_PORT"]),
        database=os.environ["MEMOS_TEST_POSTGRES_DB"],
    )

    admin_conn = psycopg2.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        dbname=url.database,
    )
    admin_conn.autocommit = True
    try:
        with admin_conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    except Exception:
        admin_conn.close()
        raise

    engines = []

    def register(engine) -> None:
        engines.append(engine)

    yield SimpleNamespace(url=url, schema=schema, register=register)

    for engine in engines:
        engine.dispose()
    with admin_conn.cursor() as cur:
        cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    admin_conn.close()


def _pg_rows(env, query, params=()):
    conn = psycopg2.connect(
        host=env.url.host,
        port=env.url.port,
        user=env.url.username,
        password=env.url.password,
        dbname=env.url.database,
        options=f"-csearch_path={env.schema}",
    )
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()
    finally:
        conn.close()


@pytest.fixture
def clock():
    return FakeClock(start=EPOCH)


@pytest.fixture
def tokens(clock):
    return WebTokenService(clock=clock)


@pytest.fixture
def store(postgres_test_schema, clock):
    s = WebSessionStore(
        database_url=postgres_test_schema.url,
        schema=postgres_test_schema.schema,
        clock=clock,
    )
    postgres_test_schema.register(s.engine)
    yield s
    s.close()


def _issue_session(store, tokens, user_id="u1"):
    access = tokens.generate_access_token()
    refresh = tokens.generate_refresh_token()
    store.create_session(
        user_id=user_id,
        access_token_hash=tokens.hash_token(access),
        access_expires_at=tokens.access_expiry(),
        refresh_token_hash=tokens.hash_token(refresh),
        refresh_expires_at=tokens.refresh_expiry(),
    )
    return access, refresh


class TestPostgresWebSessionOperations:
    def test_create_and_validate_access(self, store, tokens):
        access, _ = _issue_session(store, tokens)
        record = store.validate_access(tokens.hash_token(access))
        assert record is not None
        assert record.user_id == "u1"
        assert record.revoked_at is None
        assert record.access_expires_at == EPOCH + ACCESS_TOKEN_TTL

    def test_validate_unknown_hash_returns_none(self, store, tokens):
        assert store.validate_access(tokens.hash_token(tokens.generate_access_token())) is None

    def test_validate_expired_access_returns_none(self, store, tokens, clock):
        access, _ = _issue_session(store, tokens)
        clock.advance(ACCESS_TOKEN_TTL.total_seconds() - 1)
        assert store.validate_access(tokens.hash_token(access)) is not None
        clock.advance(2)
        assert store.validate_access(tokens.hash_token(access)) is None

    def test_revoke_and_revalidate(self, store, tokens):
        access, _ = _issue_session(store, tokens)
        family = store.validate_access(tokens.hash_token(access)).session_family_id
        assert store.revoke(family) is True
        assert store.validate_access(tokens.hash_token(access)) is None
        assert store.revoke(family) is False  # already revoked

    def test_revoke_user_sessions_revokes_all_active(self, store, tokens):
        for _ in range(3):
            _issue_session(store, tokens, user_id="alice")
        _issue_session(store, tokens, user_id="bob")
        assert store.revoke_user_sessions("alice") == 3
        assert store.revoke_user_sessions("alice") == 0
        assert store.revoke_user_sessions("bob") == 1

    def test_rotate_refresh_swaps_both_tokens_atomically(self, store, tokens, clock):
        access, refresh = _issue_session(store, tokens)
        family = store.validate_access(tokens.hash_token(access)).session_family_id

        clock.advance(60)
        new_access = tokens.generate_access_token()
        new_refresh = tokens.generate_refresh_token()
        ok = store.rotate_refresh(
            session_family_id=family,
            expected_refresh_hash=tokens.hash_token(refresh),
            new_access_token_hash=tokens.hash_token(new_access),
            new_access_expires_at=tokens.access_expiry(),
            new_refresh_token_hash=tokens.hash_token(new_refresh),
            new_refresh_expires_at=tokens.refresh_expiry(),
        )
        assert ok is True
        assert store.validate_access(tokens.hash_token(access)) is None
        record = store.validate_access(tokens.hash_token(new_access))
        assert record is not None
        assert record.previous_refresh_hash == tokens.hash_token(refresh)
        assert record.rotated_at == EPOCH + timedelta(seconds=60)

    def test_rotate_with_stale_refresh_hash_is_rejected(self, store, tokens):
        access, refresh = _issue_session(store, tokens)
        family = store.validate_access(tokens.hash_token(access)).session_family_id
        stale = tokens.hash_token(refresh)

        first_access = tokens.generate_access_token()
        first_refresh = tokens.generate_refresh_token()
        assert (
            store.rotate_refresh(
                family,
                stale,
                tokens.hash_token(first_access),
                tokens.access_expiry(),
                tokens.hash_token(first_refresh),
                tokens.refresh_expiry(),
            )
            is True
        )
        replay_access = tokens.generate_access_token()
        assert (
            store.rotate_refresh(
                family,
                stale,
                tokens.hash_token(replay_access),
                tokens.access_expiry(),
                tokens.hash_token(tokens.generate_refresh_token()),
                tokens.refresh_expiry(),
            )
            is False
        )
        assert store.validate_access(tokens.hash_token(replay_access)) is None
        assert store.validate_access(tokens.hash_token(first_access)) is not None

    def test_consume_refresh_rotate_recover_and_reuse(self, store, tokens, clock):
        _, first_refresh = _issue_session(store, tokens)
        outcome1 = store.consume_refresh(
            refresh_token_hash=tokens.hash_token(first_refresh),
            new_access_token_hash=tokens.hash_token(tokens.generate_access_token()),
            new_access_expires_at=tokens.access_expiry(),
            new_refresh_token_hash=tokens.hash_token(tokens.generate_refresh_token()),
        )
        assert outcome1.status is RotateStatus.ROTATED

        clock.advance(5)  # inside the 10s grace window
        outcome2 = store.consume_refresh(
            refresh_token_hash=tokens.hash_token(first_refresh),
            new_access_token_hash=tokens.hash_token(tokens.generate_access_token()),
            new_access_expires_at=tokens.access_expiry(),
            new_refresh_token_hash=tokens.hash_token(tokens.generate_refresh_token()),
        )
        assert outcome2.status is RotateStatus.RECOVERED

        clock.advance(30)  # outside the 30s cap
        replay = store.consume_refresh(
            refresh_token_hash=tokens.hash_token(first_refresh),
            new_access_token_hash=tokens.hash_token(tokens.generate_access_token()),
            new_access_expires_at=tokens.access_expiry(),
            new_refresh_token_hash=tokens.hash_token(tokens.generate_refresh_token()),
        )
        assert replay.status is RotateStatus.REUSED

    def test_cleanup_expired_is_bounded_and_keeps_live(self, store, tokens, clock):
        fresh_access, _ = _issue_session(store, tokens, user_id="fresh")

        def issue_expired(user_id, expired_days_ago):
            access = tokens.generate_access_token()
            refresh = tokens.generate_refresh_token()
            store.create_session(
                user_id=user_id,
                access_token_hash=tokens.hash_token(access),
                access_expires_at=clock.now() - timedelta(days=1),
                refresh_token_hash=tokens.hash_token(refresh),
                refresh_expires_at=clock.now() - timedelta(days=expired_days_ago),
            )

        for i in range(5):
            issue_expired(f"stale-{i}", expired_days_ago=31 + i)

        assert store.cleanup_expired(limit=2) == 2
        assert store.cleanup_expired(limit=100) == 3
        assert store.validate_access(tokens.hash_token(fresh_access)) is not None

    def test_timestamps_are_utc_aware(self, store, tokens):
        access, _ = _issue_session(store, tokens)
        record = store.validate_access(tokens.hash_token(access))
        for value in (record.access_expires_at, record.refresh_expires_at, record.created_at):
            assert value.tzinfo is not None
            assert value.utcoffset() == timedelta(0)


class TestPostgresWebSessionStorage:
    def test_only_hashes_stored_not_plaintext(self, store, tokens, postgres_test_schema):
        access, refresh = _issue_session(store, tokens, user_id="alice")
        family = store.validate_access(tokens.hash_token(access)).session_family_id

        rows = _pg_rows(
            postgres_test_schema,
            "SELECT * FROM web_sessions WHERE session_family_id = %s",
            (family,),
        )
        assert len(rows) == 1
        joined = " | ".join(str(value) for value in rows[0])
        assert access not in joined
        assert refresh not in joined
        assert tokens.hash_token(access) in joined
        assert tokens.hash_token(refresh) in joined

    def test_cross_instance_visibility(self, postgres_test_schema, clock):
        tokens = WebTokenService(clock=clock)
        store1 = WebSessionStore(
            database_url=postgres_test_schema.url,
            schema=postgres_test_schema.schema,
            clock=clock,
        )
        postgres_test_schema.register(store1.engine)
        access, _ = _issue_session(store1, tokens, user_id="alice")

        # A second store instance on the same schema sees the session: state
        # lives in PostgreSQL, not in a per-process local file.
        store2 = WebSessionStore(
            database_url=postgres_test_schema.url,
            schema=postgres_test_schema.schema,
            clock=clock,
        )
        postgres_test_schema.register(store2.engine)
        try:
            record = store2.validate_access(tokens.hash_token(access))
            assert record is not None
            assert record.user_id == "alice"
        finally:
            store1.close()
            store2.close()

    def test_close_is_idempotent(self, store):
        store.close()
        store.close()  # must not raise

    def test_ensure_schema_is_idempotent(self, store):
        store._ensure_schema()
        store._ensure_schema()  # must not raise
