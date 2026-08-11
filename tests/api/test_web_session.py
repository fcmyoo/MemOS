"""Tests for Web console auth primitives (phases 1+2).

Covers, per docs/plans/web-console-session-design.md and the phase 1+2 task
contract:

- injectable, testable clock (wall + monotonic time are mockable);
- ``WebTokenService``: ``wca_``/``wcr_`` ``selector.secret`` tokens, SHA-256
  storage hashing, format validation, TTLs driven by the injected clock;
- ``WebPasswordService``: Argon2id hash/verify/needs_rehash with a clear
  error when argon2-cffi is not installed;
- ``User.password_hash`` column migration on legacy SQLite databases
  (``ALTER TABLE ADD COLUMN`` + idempotent ``PRAGMA user_version=2``);
- ``WebSessionStore``: ``web_sessions`` table/index contract, create /
  validate / rotate / revoke operations, multi-worker atomic rotation and
  bounded expiry cleanup.

All time-dependent assertions use ``FakeClock`` — no real sleeping.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading

from datetime import UTC, datetime, timedelta

import pytest

from memos.api import web_auth
from memos.api.web_auth import (
    ACCESS_TOKEN_PREFIX,
    ACCESS_TOKEN_TTL,
    MAX_ACTIVE_FAMILIES,
    PREVIOUS_REFRESH_GRACE,
    REFRESH_TOKEN_PREFIX,
    REFRESH_TOKEN_TTL,
    Argon2NotInstalledError,
    ClockService,
    FakeClock,
    RotateStatus,
    SessionService,
    WebAuthError,
    WebPasswordService,
    WebRateLimiter,
    WebSessionStore,
    WebTokenService,
)


EPOCH = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Clock: injectable and mockable
# ---------------------------------------------------------------------------


class TestClock:
    def test_system_clock_returns_aware_utc_now(self):
        clock = ClockService()
        before = datetime.now(UTC)
        got = clock.now()
        after = datetime.now(UTC)
        assert got.tzinfo is not None
        assert got.utcoffset() == timedelta(0)
        assert before <= got <= after
        assert isinstance(clock.monotonic(), float)

    def test_fake_clock_is_frozen_until_advanced(self):
        clock = FakeClock(start=EPOCH)
        assert clock.now() == EPOCH
        assert clock.now() == EPOCH  # wall time does not drift
        mono0 = clock.monotonic()
        clock.advance(90)
        assert clock.now() == EPOCH + timedelta(seconds=90)
        assert clock.monotonic() == mono0 + 90

    def test_fake_clock_monotonic_tracks_wall_time(self):
        clock = FakeClock(start=EPOCH, monotonic_start=1000.0)
        assert clock.monotonic() == 1000.0
        clock.advance(0.5)
        clock.advance(0.25)
        assert clock.monotonic() == pytest.approx(1000.75)
        assert clock.now() == EPOCH + timedelta(seconds=0.75)


# ---------------------------------------------------------------------------
# WebTokenService: token format, hashing, TTLs
# ---------------------------------------------------------------------------


class TestWebTokenService:
    def test_access_token_has_wca_prefix_and_selector_secret_shape(self):
        tokens = WebTokenService(clock=FakeClock(start=EPOCH))
        token = tokens.generate_access_token()
        assert token.startswith(ACCESS_TOKEN_PREFIX)
        body = token[len(ACCESS_TOKEN_PREFIX):]
        selector, _, secret = body.partition(".")
        # 128-bit selector, 256-bit secret, lowercase hex
        assert len(selector) == 32
        assert len(secret) == 64
        int(selector, 16)
        int(secret, 16)
        assert token == token.lower()

    def test_refresh_token_has_wcr_prefix(self):
        tokens = WebTokenService()
        assert tokens.generate_refresh_token().startswith(REFRESH_TOKEN_PREFIX)

    def test_generated_tokens_are_unique_csprng_output(self):
        tokens = WebTokenService()
        seen = {tokens.generate_access_token() for _ in range(64)}
        assert len(seen) == 64

    def test_hash_token_is_sha256_hex_and_never_the_plaintext(self):
        tokens = WebTokenService()
        token = tokens.generate_access_token()
        digest = tokens.hash_token(token)
        assert digest == hashlib.sha256(token.encode("utf-8")).hexdigest()
        assert digest != token
        assert len(digest) == 64

    def test_verify_hash_compares_in_constant_time(self):
        tokens = WebTokenService()
        token = tokens.generate_refresh_token()
        digest = tokens.hash_token(token)
        assert tokens.verify_hash(token, digest) is True
        assert tokens.verify_hash(token + "x", digest) is False
        assert tokens.verify_hash(token, "0" * 64) is False

    @pytest.mark.parametrize(
        "bad_token",
        [
            "",
            "wca_nothing",
            "krlk_" + "a" * 64,  # API-key format must not be accepted
            "wcx_" + "a" * 32 + "." + "b" * 64,  # wrong prefix
            ACCESS_TOKEN_PREFIX + "a" * 31 + "." + "b" * 64,  # short selector
            ACCESS_TOKEN_PREFIX + "a" * 32 + "." + "b" * 63,  # short secret
            ACCESS_TOKEN_PREFIX + "a" * 32 + "b" * 64,  # missing dot
            ACCESS_TOKEN_PREFIX + "g" * 32 + "." + "b" * 64,  # non-hex
            REFRESH_TOKEN_PREFIX + "A" * 32 + "." + "B" * 64,  # uppercase
        ],
    )
    def test_parse_rejects_malformed_tokens(self, bad_token):
        with pytest.raises(ValueError):
            WebTokenService.parse_token(bad_token)

    def test_parse_accepts_both_prefixes_and_reports_kind(self):
        tokens = WebTokenService()
        access = tokens.generate_access_token()
        refresh = tokens.generate_refresh_token()
        kind_a, selector_a, secret_a = WebTokenService.parse_token(access)
        kind_r, _, _ = WebTokenService.parse_token(refresh)
        assert kind_a == "access"
        assert kind_r == "refresh"
        assert selector_a in access
        assert secret_a in access

    def test_ttls_are_contract_values(self):
        assert ACCESS_TOKEN_TTL == timedelta(minutes=15)
        assert REFRESH_TOKEN_TTL == timedelta(days=30)

    def test_expiries_follow_the_injected_clock(self):
        clock = FakeClock(start=EPOCH)
        tokens = WebTokenService(clock=clock)
        assert tokens.access_expiry() == EPOCH + ACCESS_TOKEN_TTL
        assert tokens.refresh_expiry() == EPOCH + REFRESH_TOKEN_TTL
        clock.advance(3600)
        assert tokens.access_expiry() == EPOCH + timedelta(hours=1) + ACCESS_TOKEN_TTL


# ---------------------------------------------------------------------------
# WebPasswordService: Argon2id (dependency optional at runtime)
# ---------------------------------------------------------------------------


class _FakeArgon2Hasher:
    """Stand-in for argon2.PasswordHasher so logic is testable without the
    native dependency installed."""

    def __init__(self, needs_rehash: bool = False):
        self.needs_rehash = needs_rehash
        self.hash_calls: list[str] = []

    def hash(self, password: str) -> str:
        self.hash_calls.append(password)
        return f"$argon2id$v=19$fake${hashlib.sha256(password.encode()).hexdigest()}"

    def verify(self, password_hash: str, password: str) -> bool:
        expected = f"$argon2id$v=19$fake${hashlib.sha256(password.encode()).hexdigest()}"
        if password_hash != expected:
            raise ValueError("password does not match")
        return True

    def check_needs_rehash(self, password_hash: str) -> bool:
        return self.needs_rehash


class TestWebPasswordService:
    def test_argon2_parameters_match_contract(self):
        assert web_auth.ARGON2_MEMORY_COST_KIB == 64 * 1024  # 64 MiB
        assert web_auth.ARGON2_TIME_COST == 3
        assert web_auth.ARGON2_PARALLELISM == 4
        assert web_auth.ARGON2_HASH_LEN == 32
        assert web_auth.ARGON2_SALT_LEN == 16

    def test_hash_produces_phc_string(self):
        service = WebPasswordService(hasher=_FakeArgon2Hasher())
        encoded = service.hash_password("s3cret-pass")
        assert encoded.startswith("$argon2id$")
        assert "s3cret-pass" not in encoded

    def test_verify_accepts_correct_and_rejects_wrong_password(self):
        service = WebPasswordService(hasher=_FakeArgon2Hasher())
        encoded = service.hash_password("correct horse")
        assert service.verify_password(encoded, "correct horse") is True
        assert service.verify_password(encoded, "wrong horse") is False
        assert service.verify_password("not-a-valid-hash", "x") is False

    def test_needs_rehash_delegates_to_hasher(self):
        stale = _FakeArgon2Hasher(needs_rehash=True)
        fresh = _FakeArgon2Hasher(needs_rehash=False)
        assert WebPasswordService(hasher=stale).needs_rehash("$argon2id$x") is True
        assert WebPasswordService(hasher=fresh).needs_rehash("$argon2id$x") is False

    def test_missing_argon2_dependency_raises_actionable_error(self, monkeypatch):
        monkeypatch.setattr(web_auth, "_ARGON2_AVAILABLE", False)
        with pytest.raises(Argon2NotInstalledError) as excinfo:
            WebPasswordService()
        message = str(excinfo.value)
        assert "argon2-cffi" in message
        assert isinstance(excinfo.value, RuntimeError)

    def test_missing_dependency_is_noted_on_import_when_absent(self):
        # Whether the dependency is installed or not, the module must expose
        # the availability flag for the router layer to surface clean errors.
        assert isinstance(web_auth._ARGON2_AVAILABLE, bool)


# ---------------------------------------------------------------------------
# User.password_hash migration (legacy DB compatibility)
# ---------------------------------------------------------------------------


LEGACY_USERS_DDL = """
CREATE TABLE users (
    user_id VARCHAR NOT NULL,
    user_name VARCHAR NOT NULL,
    role VARCHAR NOT NULL,
    created_at DATETIME,
    updated_at DATETIME,
    is_active BOOLEAN NOT NULL,
    PRIMARY KEY (user_id),
    UNIQUE (user_name)
)
"""


def _make_legacy_db(path) -> str:
    conn = sqlite3.connect(path)
    try:
        conn.execute(LEGACY_USERS_DDL)
        conn.execute(
            "INSERT INTO users (user_id, user_name, role, created_at, updated_at, is_active) "
            "VALUES ('u-legacy', 'legacy', 'USER', '2024-01-01 00:00:00', '2024-01-01 00:00:00', 1)"
        )
        conn.commit()
    finally:
        conn.close()
    return str(path)


def _sqlite_scalar(path, sql):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _column_names(path, table: str) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


class TestPasswordHashMigration:
    def test_legacy_db_gains_password_hash_column(self, tmp_path):
        from memos.mem_user.user_manager import UserManager

        db_path = _make_legacy_db(tmp_path / "memos_users.db")
        assert "password_hash" not in _column_names(db_path, "users")

        UserManager(db_path=db_path)

        assert "password_hash" in _column_names(db_path, "users")
        assert _sqlite_scalar(db_path, "PRAGMA user_version") == 2

    def test_legacy_rows_survive_migration(self, tmp_path):
        from memos.mem_user.user_manager import UserManager

        db_path = _make_legacy_db(tmp_path / "memos_users.db")
        manager = UserManager(db_path=db_path)
        user = manager.get_user("u-legacy")
        assert user is not None
        assert user.user_name == "legacy"
        assert user.password_hash is None  # nullable for historical users

    def test_fresh_db_has_column_and_version_without_alter(self, tmp_path):
        from memos.mem_user.user_manager import UserManager

        db_path = str(tmp_path / "fresh.db")
        UserManager(db_path=db_path)
        assert "password_hash" in _column_names(db_path, "users")
        assert _sqlite_scalar(db_path, "PRAGMA user_version") == 2

    def test_migration_is_idempotent_across_restarts(self, tmp_path):
        from memos.mem_user.user_manager import UserManager

        db_path = _make_legacy_db(tmp_path / "memos_users.db")
        UserManager(db_path=db_path)
        UserManager(db_path=db_path)  # second boot must not fail or double-apply
        assert "password_hash" in _column_names(db_path, "users")
        assert _sqlite_scalar(db_path, "PRAGMA user_version") == 2
        legacy_count = _sqlite_scalar(
            db_path, "SELECT COUNT(*) FROM users WHERE user_id='u-legacy'"
        )
        assert legacy_count == 1

    def test_password_hash_roundtrip_via_model(self, tmp_path):
        from memos.mem_user.user_manager import User, UserManager

        db_path = str(tmp_path / "model.db")
        manager = UserManager(db_path=db_path)
        user_id = manager.create_user(user_name="webuser")
        session = manager._get_session()
        try:
            session.query(User).filter(User.user_id == user_id).update(
                {User.password_hash: "$argon2id$v=19$xyz"}
            )
            session.commit()
        finally:
            session.close()
        reloaded = manager.get_user(user_id)
        assert reloaded.password_hash == "$argon2id$v=19$xyz"


# ---------------------------------------------------------------------------
# WebSessionStore: schema contract
# ---------------------------------------------------------------------------

EXPECTED_SESSION_COLUMNS = {
    "session_family_id",
    "user_id",
    "access_token_hash",
    "access_expires_at",
    "refresh_token_hash",
    "previous_refresh_hash",
    "previous_refresh_valid_until",
    "refresh_expires_at",
    "rotation_counter",
    "rotated_at",
    "last_refreshed_at",
    "revoked_at",
    "revoke_reason",
    "created_at",
    "last_seen_at",
}


class TestWebSessionSchema:
    def test_table_created_with_contract_columns(self, tmp_path):
        store = WebSessionStore(db_path=str(tmp_path / "sessions.db"))
        columns = _column_names(str(tmp_path / "sessions.db"), "web_sessions")
        assert set(columns) == EXPECTED_SESSION_COLUMNS
        # session_family_id is the primary key
        conn = sqlite3.connect(str(tmp_path / "sessions.db"))
        try:
            info = conn.execute("PRAGMA table_info(web_sessions)").fetchall()
        finally:
            conn.close()
        pk_cols = [row[1] for row in info if row[5] > 0]
        assert pk_cols == ["session_family_id"]

    def test_active_sessions_index_created_idempotently(self, tmp_path):
        db = str(tmp_path / "sessions.db")
        WebSessionStore(db_path=db)
        WebSessionStore(db_path=db)  # re-ensure must not raise
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='idx_web_sessions_user_active'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        index_sql = rows[0][0].lower()
        assert "user_id" in index_sql
        assert "revoked_at" in index_sql
        assert "refresh_expires_at" in index_sql

    def test_connection_pragmas_for_multi_worker_safety(self, tmp_path):
        store = WebSessionStore(db_path=str(tmp_path / "sessions.db"))
        conn = store._connect()
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# WebSessionStore: operations with injected clock
# ---------------------------------------------------------------------------


def _issue_session(store: WebSessionStore, tokens: WebTokenService, user_id: str = "u1"):
    """Create a session from freshly generated tokens; return (access, refresh)."""
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


class TestWebSessionOperations:
    @pytest.fixture()
    def clock(self):
        return FakeClock(start=EPOCH)

    @pytest.fixture()
    def store(self, tmp_path, clock):
        return WebSessionStore(db_path=str(tmp_path / "sessions.db"), clock=clock)

    @pytest.fixture()
    def tokens(self, clock):
        return WebTokenService(clock=clock)

    def test_create_and_validate_access(self, store, tokens):
        access, _ = _issue_session(store, tokens)
        record = store.validate_access(tokens.hash_token(access))
        assert record is not None
        assert record.user_id == "u1"
        assert record.revoked_at is None
        assert record.access_expires_at == EPOCH + ACCESS_TOKEN_TTL
        assert record.created_at == EPOCH

    def test_validate_unknown_hash_returns_none(self, store, tokens):
        assert store.validate_access(tokens.hash_token(tokens.generate_access_token())) is None

    def test_validate_expired_access_returns_none(self, store, tokens, clock):
        access, _ = _issue_session(store, tokens)
        clock.advance(ACCESS_TOKEN_TTL.total_seconds() - 1)
        assert store.validate_access(tokens.hash_token(access)) is not None
        clock.advance(2)
        assert store.validate_access(tokens.hash_token(access)) is None

    def test_validate_revoked_session_returns_none(self, store, tokens):
        access, refresh = _issue_session(store, tokens)
        record = store.validate_access(tokens.hash_token(access))
        assert store.revoke(record.session_family_id) is True
        assert store.validate_access(tokens.hash_token(access)) is None
        assert store.revoke(record.session_family_id) is False  # already revoked

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

        # Old access token is dead, new one validates.
        assert store.validate_access(tokens.hash_token(access)) is None
        record = store.validate_access(tokens.hash_token(new_access))
        assert record is not None
        assert record.previous_refresh_hash == tokens.hash_token(refresh)
        assert record.rotated_at == EPOCH + timedelta(seconds=60)
        assert record.last_seen_at == EPOCH + timedelta(seconds=60)

    def test_rotate_with_stale_refresh_hash_is_rejected(self, store, tokens):
        access, refresh = _issue_session(store, tokens)
        family = store.validate_access(tokens.hash_token(access)).session_family_id
        stale_hash = tokens.hash_token(refresh)

        first_access = tokens.generate_access_token()
        first_refresh = tokens.generate_refresh_token()
        assert (
            store.rotate_refresh(
                family,
                stale_hash,
                tokens.hash_token(first_access),
                tokens.access_expiry(),
                tokens.hash_token(first_refresh),
                tokens.refresh_expiry(),
            )
            is True
        )

        # Replaying the pre-rotation refresh hash must fail and leave state intact.
        replay_access = tokens.generate_access_token()
        assert (
            store.rotate_refresh(
                family,
                stale_hash,
                tokens.hash_token(replay_access),
                tokens.access_expiry(),
                tokens.hash_token(tokens.generate_refresh_token()),
                tokens.refresh_expiry(),
            )
            is False
        )
        assert store.validate_access(tokens.hash_token(replay_access)) is None
        assert store.validate_access(tokens.hash_token(first_access)) is not None

    def test_rotate_revoked_session_fails(self, store, tokens):
        access, refresh = _issue_session(store, tokens)
        family = store.validate_access(tokens.hash_token(access)).session_family_id
        store.revoke(family)
        assert (
            store.rotate_refresh(
                family,
                tokens.hash_token(refresh),
                tokens.hash_token(tokens.generate_access_token()),
                tokens.access_expiry(),
                tokens.hash_token(tokens.generate_refresh_token()),
                tokens.refresh_expiry(),
            )
            is False
        )

    def test_timestamps_are_stored_and_returned_as_utc_aware(self, store, tokens):
        access, _ = _issue_session(store, tokens)
        record = store.validate_access(tokens.hash_token(access))
        for value in (record.access_expires_at, record.refresh_expires_at, record.created_at):
            assert value.tzinfo is not None
            assert value.utcoffset() == timedelta(0)

    def test_concurrent_rotate_only_one_wins(self, tmp_path, clock):
        db = str(tmp_path / "sessions.db")
        tokens = WebTokenService(clock=clock)
        store = WebSessionStore(db_path=db, clock=clock)
        access, refresh = _issue_session(store, tokens)
        family = store.validate_access(tokens.hash_token(access)).session_family_id
        expected_hash = tokens.hash_token(refresh)

        results: list[bool] = []
        barrier = threading.Barrier(2)

        def worker(idx: int):
            worker_clock = FakeClock(start=EPOCH)
            worker_tokens = WebTokenService(clock=worker_clock)
            worker_store = WebSessionStore(db_path=db, clock=worker_clock)
            barrier.wait()
            results.append(
                worker_store.rotate_refresh(
                    family,
                    expected_hash,
                    worker_tokens.hash_token(worker_tokens.generate_access_token()),
                    worker_tokens.access_expiry(),
                    worker_tokens.hash_token(worker_tokens.generate_refresh_token()),
                    worker_tokens.refresh_expiry(),
                )
            )

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert sorted(results) == [False, True]  # exactly one CAS wins
        conn = sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT previous_refresh_hash, rotated_at FROM web_sessions "
                "WHERE session_family_id = ?",
                (family,),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1
        assert rows[0][0] == expected_hash
        assert rows[0][1] is not None  # no half-update: rotation fully applied once

    def test_cleanup_expired_is_bounded_and_keeps_live_sessions(self, store, tokens, clock):
        fresh_access, _ = _issue_session(store, tokens, user_id="fresh")

        def issue_expired(user_id: str, expired_days_ago: float):
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

        removed = store.cleanup_expired(limit=2)
        assert removed == 2
        removed = store.cleanup_expired(limit=100)
        assert removed == 3
        # Live session survives cleanup.
        assert store.validate_access(tokens.hash_token(fresh_access)) is not None
        assert store.cleanup_expired(limit=100) == 0


# ---------------------------------------------------------------------------
# Phase 3: schema growth on batch-1 databases (idempotent column migration)
# ---------------------------------------------------------------------------

# Exact web_sessions DDL as shipped by batch 1 (phases 1+2). Phase 3 adds the
# grace-window, rotation-counter and revoke-reason columns on top of it.
BATCH1_SESSIONS_DDL = """
CREATE TABLE web_sessions (
    session_family_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    access_token_hash TEXT NOT NULL UNIQUE,
    access_expires_at TEXT NOT NULL,
    refresh_token_hash TEXT NOT NULL UNIQUE,
    previous_refresh_hash TEXT,
    refresh_expires_at TEXT NOT NULL,
    rotated_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
)
"""


class TestPhase3SchemaMigration:
    def test_batch1_db_gains_phase3_columns_with_data_intact(self, tmp_path):
        db = str(tmp_path / "sessions.db")
        conn = sqlite3.connect(db)
        try:
            conn.execute(BATCH1_SESSIONS_DDL)
            conn.execute(
                "INSERT INTO web_sessions ("
                "session_family_id, user_id, access_token_hash, access_expires_at, "
                "refresh_token_hash, refresh_expires_at, created_at, last_seen_at"
                ") VALUES ('fam-1', 'u1', 'a1', '2026-01-01T00:15:00.000000+00:00', "
                "'r1', '2026-01-31T00:00:00.000000+00:00', "
                "'2026-01-01T00:00:00.000000+00:00', '2026-01-01T00:00:00.000000+00:00')"
            )
            conn.commit()
        finally:
            conn.close()

        store = WebSessionStore(db_path=db)

        assert set(_column_names(db, "web_sessions")) == EXPECTED_SESSION_COLUMNS
        record = store.get_session("fam-1")
        assert record is not None
        assert record.user_id == "u1"
        assert record.rotation_counter == 0  # backfilled default
        assert record.previous_refresh_valid_until is None
        assert record.last_refreshed_at is None
        assert record.revoke_reason is None

    def test_phase3_migration_is_idempotent(self, tmp_path):
        db = str(tmp_path / "sessions.db")
        conn = sqlite3.connect(db)
        try:
            conn.execute(BATCH1_SESSIONS_DDL)
            conn.commit()
        finally:
            conn.close()
        WebSessionStore(db_path=db)
        WebSessionStore(db_path=db)  # re-ensure must not raise
        assert set(_column_names(db, "web_sessions")) == EXPECTED_SESSION_COLUMNS


# ---------------------------------------------------------------------------
# Phase 3: rotation with grace window and replay detection (store level)
# ---------------------------------------------------------------------------


class TestConsumeRefresh:
    @pytest.fixture()
    def clock(self):
        return FakeClock(start=EPOCH)

    @pytest.fixture()
    def store(self, tmp_path, clock):
        return WebSessionStore(db_path=str(tmp_path / "sessions.db"), clock=clock)

    @pytest.fixture()
    def tokens(self, clock):
        return WebTokenService(clock=clock)

    def _consume(self, store, tokens, old_refresh, new_access=None, new_refresh=None):
        new_access = new_access or tokens.generate_access_token()
        new_refresh = new_refresh or tokens.generate_refresh_token()
        outcome = store.consume_refresh(
            refresh_token_hash=tokens.hash_token(old_refresh),
            new_access_token_hash=tokens.hash_token(new_access),
            new_access_expires_at=tokens.access_expiry(),
            new_refresh_token_hash=tokens.hash_token(new_refresh),
        )
        return outcome, new_access, new_refresh

    def test_grace_constant_matches_contract(self):
        assert PREVIOUS_REFRESH_GRACE == timedelta(seconds=10)

    def test_rotate_swaps_tokens_and_invalidates_old_access(self, store, tokens, clock):
        old_access, old_refresh = _issue_session(store, tokens)
        clock.advance(60)

        outcome, new_access, new_refresh = self._consume(store, tokens, old_refresh)

        assert outcome.status is RotateStatus.ROTATED
        assert outcome.rotation_counter == 1
        assert outcome.refresh_expires_at == EPOCH + REFRESH_TOKEN_TTL  # not extended
        # Old access is dead, new one validates.
        assert store.validate_access(tokens.hash_token(old_access)) is None
        assert store.validate_access(tokens.hash_token(new_access)) is not None
        # Old refresh hash moved into the grace slot with a deadline.
        record = store.get_session(outcome.session_family_id)
        assert record.refresh_token_hash == tokens.hash_token(new_refresh)
        assert record.previous_refresh_hash == tokens.hash_token(old_refresh)
        assert record.previous_refresh_valid_until == (
            EPOCH + timedelta(seconds=60) + PREVIOUS_REFRESH_GRACE
        )
        assert record.last_refreshed_at == EPOCH + timedelta(seconds=60)
        assert record.last_seen_at == EPOCH + timedelta(seconds=60)
        assert record.rotation_counter == 1

    def test_rotation_never_extends_absolute_refresh_expiry(self, store, tokens, clock):
        _, refresh = _issue_session(store, tokens)
        absolute_expiry = EPOCH + REFRESH_TOKEN_TTL
        clock.advance(5 * 86400)  # 5 days later

        outcome, _, _ = self._consume(store, tokens, refresh)

        assert outcome.status is RotateStatus.ROTATED
        assert outcome.refresh_expires_at == absolute_expiry
        record = store.get_session(outcome.session_family_id)
        assert record.refresh_expires_at == absolute_expiry

    def test_previous_refresh_recovers_within_grace_window(self, store, tokens, clock):
        _, first_refresh = _issue_session(store, tokens)
        outcome1, _, second_refresh = self._consume(store, tokens, first_refresh)
        assert outcome1.status is RotateStatus.ROTATED

        clock.advance(PREVIOUS_REFRESH_GRACE.total_seconds() - 1)
        # Lost-response / concurrent-tab recovery: replay the previous token.
        outcome2, new_access, third_refresh = self._consume(store, tokens, first_refresh)

        assert outcome2.status is RotateStatus.RECOVERED
        assert outcome2.rotation_counter == 2
        assert store.validate_access(tokens.hash_token(new_access)) is not None
        record = store.get_session(outcome2.session_family_id)
        # Only the immediate previous generation is retained.
        assert record.previous_refresh_hash == tokens.hash_token(second_refresh)

    def test_previous_refresh_recovers_only_once(self, store, tokens, clock):
        _, first_refresh = _issue_session(store, tokens)
        self._consume(store, tokens, first_refresh)
        clock.advance(1)
        outcome = self._consume(store, tokens, first_refresh)[0]
        assert outcome.status is RotateStatus.RECOVERED

        # Second replay of the same previous token is reuse -> family revoked.
        clock.advance(1)
        replay = self._consume(store, tokens, first_refresh)[0]
        assert replay.status is RotateStatus.REUSED
        record = store.get_session(outcome.session_family_id)
        assert record.revoked_at is not None
        assert record.revoke_reason == "refresh_reuse"

    def test_replay_outside_grace_revokes_family(self, store, tokens, clock):
        _, first_refresh = _issue_session(store, tokens)
        outcome, new_access, _ = self._consume(store, tokens, first_refresh)
        assert outcome.status is RotateStatus.ROTATED

        clock.advance(PREVIOUS_REFRESH_GRACE.total_seconds() + 1)
        replay, replay_access, _ = self._consume(
            store, tokens, first_refresh, new_access=tokens.generate_access_token()
        )

        assert replay.status is RotateStatus.REUSED
        record = store.get_session(outcome.session_family_id)
        assert record.revoked_at is not None
        assert record.revoke_reason == "refresh_reuse"
        # Rotation state untouched: the legitimate current access still validates
        # only until the revocation is observed.
        assert store.validate_access(tokens.hash_token(new_access)) is None
        assert store.validate_access(tokens.hash_token(replay_access)) is None

    def test_reuse_sets_revoked_at_atomically(self, store, tokens, clock):
        _, first_refresh = _issue_session(store, tokens)
        self._consume(store, tokens, first_refresh)
        clock.advance(30)

        self._consume(store, tokens, first_refresh)

        conn = sqlite3.connect(store.db_path)
        try:
            row = conn.execute(
                "SELECT revoked_at, revoke_reason FROM web_sessions"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] is not None
        assert row[1] == "refresh_reuse"

    def test_unknown_refresh_hash_is_invalid_without_side_effects(self, store, tokens):
        access, _ = _issue_session(store, tokens)
        outcome = self._consume(store, tokens, tokens.generate_refresh_token())[0]
        assert outcome.status is RotateStatus.INVALID
        assert store.validate_access(tokens.hash_token(access)) is not None

    def test_expired_refresh_reports_expired_and_does_not_revoke(self, store, tokens, clock):
        _, refresh = _issue_session(store, tokens)
        family = store.get_by_refresh_hash(tokens.hash_token(refresh)).session_family_id
        clock.advance(REFRESH_TOKEN_TTL.total_seconds() + 1)

        outcome = self._consume(store, tokens, refresh)[0]

        assert outcome.status is RotateStatus.EXPIRED
        assert store.get_session(family).revoked_at is None

    def test_revoked_family_reports_revoked(self, store, tokens):
        access, refresh = _issue_session(store, tokens)
        family = store.validate_access(tokens.hash_token(access)).session_family_id
        store.revoke(family)

        outcome = self._consume(store, tokens, refresh)[0]

        assert outcome.status is RotateStatus.REVOKED

    def test_reuse_of_one_family_does_not_touch_others(self, store, tokens, clock):
        access_a, refresh_a = _issue_session(store, tokens, user_id="alice")
        access_b, _ = _issue_session(store, tokens, user_id="bob")
        self._consume(store, tokens, refresh_a)
        clock.advance(30)

        replay = self._consume(store, tokens, refresh_a)[0]

        assert replay.status is RotateStatus.REUSED
        assert store.validate_access(tokens.hash_token(access_a)) is None
        # Bob's session is untouched by Alice's family revocation.
        assert store.validate_access(tokens.hash_token(access_b)) is not None

    def test_grace_window_is_capped_at_30_seconds(self, store, tokens, clock):
        _, first_refresh = _issue_session(store, tokens)
        self._consume(store, tokens, first_refresh)

        clock.advance(29)  # beyond the 10s default, inside the 30s cap
        outcome = store.consume_refresh(
            refresh_token_hash=tokens.hash_token(first_refresh),
            new_access_token_hash=tokens.hash_token(tokens.generate_access_token()),
            new_access_expires_at=tokens.access_expiry(),
            new_refresh_token_hash=tokens.hash_token(tokens.generate_refresh_token()),
            grace=timedelta(seconds=120),  # must be clamped to the 30s contract cap
        )
        assert outcome.status is RotateStatus.RECOVERED

    def test_expired_grace_window_rejects_even_with_large_grace_param(
        self, store, tokens, clock
    ):
        _, first_refresh = _issue_session(store, tokens)
        self._consume(store, tokens, first_refresh)
        clock.advance(31)  # beyond the 30s cap itself

        outcome = store.consume_refresh(
            refresh_token_hash=tokens.hash_token(first_refresh),
            new_access_token_hash=tokens.hash_token(tokens.generate_access_token()),
            new_access_expires_at=tokens.access_expiry(),
            new_refresh_token_hash=tokens.hash_token(tokens.generate_refresh_token()),
            grace=timedelta(seconds=120),
        )
        assert outcome.status is RotateStatus.REUSED

    def test_find_by_access_hash_returns_row_regardless_of_state(self, store, tokens, clock):
        access, _ = _issue_session(store, tokens)
        token_hash = tokens.hash_token(access)
        assert store.find_by_access_hash(token_hash) is not None
        clock.advance(ACCESS_TOKEN_TTL.total_seconds() + 1)
        record = store.find_by_access_hash(token_hash)
        assert record is not None  # expired row still resolvable for logout
        assert store.validate_access(token_hash) is None

    def test_find_by_any_refresh_hash_matches_current_and_previous(
        self, store, tokens
    ):
        _, first_refresh = _issue_session(store, tokens)
        outcome, _, second_refresh = self._consume(store, tokens, first_refresh)
        assert (
            store.find_by_any_refresh_hash(tokens.hash_token(first_refresh))
            .session_family_id
            == outcome.session_family_id
        )
        assert (
            store.find_by_any_refresh_hash(tokens.hash_token(second_refresh))
            .session_family_id
            == outcome.session_family_id
        )
        assert store.find_by_any_refresh_hash(tokens.hash_token("wcr_" + "0" * 32 + "." + "0" * 64)) is None


# ---------------------------------------------------------------------------
# Phase 3: SessionService (issue / validate / rotate / logout / cap)
# ---------------------------------------------------------------------------


class TestSessionService:
    @pytest.fixture()
    def clock(self):
        return FakeClock(start=EPOCH)

    @pytest.fixture()
    def env(self, tmp_path, clock):
        store = WebSessionStore(db_path=str(tmp_path / "sessions.db"), clock=clock)
        tokens = WebTokenService(clock=clock)
        active_users = {"u1"}
        service = SessionService(
            store=store,
            tokens=tokens,
            is_user_active=lambda user_id: user_id in active_users,
            clock=clock,
        )
        return {"service": service, "store": store, "tokens": tokens, "active": active_users}

    def test_issue_pair_returns_fresh_pair_at_rotation_zero(self, env):
        pair = env["service"].issue_pair("u1")
        assert pair.access_token.startswith(ACCESS_TOKEN_PREFIX)
        assert pair.refresh_token.startswith(REFRESH_TOKEN_PREFIX)
        assert pair.access_expires_at == EPOCH + ACCESS_TOKEN_TTL
        assert pair.refresh_expires_at == EPOCH + REFRESH_TOKEN_TTL
        assert pair.rotation == 0
        record = env["store"].get_session(pair.session_family_id)
        assert record.user_id == "u1"
        assert record.access_token_hash == env["tokens"].hash_token(pair.access_token)

    def test_each_issue_creates_an_independent_family(self, env):
        pair_a = env["service"].issue_pair("u1")
        pair_b = env["service"].issue_pair("u1")
        assert pair_a.session_family_id != pair_b.session_family_id

    def test_validate_access_returns_record_for_live_token(self, env):
        pair = env["service"].issue_pair("u1")
        record = env["service"].validate_access(pair.access_token)
        assert record.user_id == "u1"
        assert record.session_family_id == pair.session_family_id

    @pytest.mark.parametrize("bad_token", ["", "garbage", "krlk_" + "a" * 64])
    def test_validate_access_rejects_malformed_and_api_keys(self, env, bad_token):
        with pytest.raises(WebAuthError) as excinfo:
            env["service"].validate_access(bad_token)
        assert excinfo.value.code == "access_token_invalid"

    def test_refresh_token_cannot_act_as_access(self, env):
        pair = env["service"].issue_pair("u1")
        with pytest.raises(WebAuthError) as excinfo:
            env["service"].validate_access(pair.refresh_token)
        assert excinfo.value.code == "access_token_invalid"

    def test_validate_access_expired_code(self, env, clock):
        pair = env["service"].issue_pair("u1")
        clock.advance(ACCESS_TOKEN_TTL.total_seconds() + 1)
        with pytest.raises(WebAuthError) as excinfo:
            env["service"].validate_access(pair.access_token)
        assert excinfo.value.code == "access_token_expired"

    def test_validate_access_revoked_code(self, env):
        pair = env["service"].issue_pair("u1")
        env["store"].revoke(pair.session_family_id)
        with pytest.raises(WebAuthError) as excinfo:
            env["service"].validate_access(pair.access_token)
        assert excinfo.value.code == "session_revoked"

    def test_validate_access_inactive_user_code(self, env):
        pair = env["service"].issue_pair("u1")
        env["active"].discard("u1")
        with pytest.raises(WebAuthError) as excinfo:
            env["service"].validate_access(pair.access_token)
        assert excinfo.value.code == "session_revoked"

    def test_rotate_refresh_issues_new_pair_and_kills_old_access(self, env, clock):
        pair1 = env["service"].issue_pair("u1")
        clock.advance(60)

        pair2 = env["service"].rotate_refresh(pair1.refresh_token)

        assert pair2.rotation == 1
        assert pair2.access_token != pair1.access_token
        assert pair2.refresh_token != pair1.refresh_token
        assert pair2.refresh_expires_at == pair1.refresh_expires_at  # absolute TTL fixed
        env["service"].validate_access(pair2.access_token)
        with pytest.raises(WebAuthError) as excinfo:
            env["service"].validate_access(pair1.access_token)
        assert excinfo.value.code == "access_token_invalid"  # hash no longer stored

    def test_rotate_refresh_within_grace_recovers_lost_response(self, env, clock):
        pair1 = env["service"].issue_pair("u1")
        pair2 = env["service"].rotate_refresh(pair1.refresh_token)
        clock.advance(PREVIOUS_REFRESH_GRACE.total_seconds() - 2)

        pair3 = env["service"].rotate_refresh(pair1.refresh_token)

        assert pair3.rotation == 2
        env["service"].validate_access(pair3.access_token)
        # The generation that was current before recovery stays recoverable once.
        clock.advance(1)
        pair4 = env["service"].rotate_refresh(pair2.refresh_token)
        assert pair4.rotation == 3

    def test_rotate_refresh_replay_after_grace_revokes_family(self, env, clock):
        pair1 = env["service"].issue_pair("u1")
        pair2 = env["service"].rotate_refresh(pair1.refresh_token)
        clock.advance(PREVIOUS_REFRESH_GRACE.total_seconds() + 1)

        with pytest.raises(WebAuthError) as excinfo:
            env["service"].rotate_refresh(pair1.refresh_token)
        assert excinfo.value.code == "refresh_token_reused"

        # The whole family is dead: even the legitimately issued pair2.
        with pytest.raises(WebAuthError) as excinfo:
            env["service"].validate_access(pair2.access_token)
        assert excinfo.value.code == "session_revoked"
        with pytest.raises(WebAuthError) as excinfo:
            env["service"].rotate_refresh(pair2.refresh_token)
        assert excinfo.value.code == "refresh_token_revoked"

    @pytest.mark.parametrize("bad_token", ["", "garbage", "krlk_" + "a" * 64])
    def test_rotate_refresh_rejects_malformed_tokens(self, env, bad_token):
        with pytest.raises(WebAuthError) as excinfo:
            env["service"].rotate_refresh(bad_token)
        assert excinfo.value.code == "refresh_token_invalid"

    def test_rotate_refresh_rejects_access_token_as_refresh(self, env):
        pair = env["service"].issue_pair("u1")
        with pytest.raises(WebAuthError) as excinfo:
            env["service"].rotate_refresh(pair.access_token)
        assert excinfo.value.code == "refresh_token_invalid"

    def test_rotate_refresh_unknown_token_is_invalid(self, env):
        tokens = env["tokens"]
        with pytest.raises(WebAuthError) as excinfo:
            env["service"].rotate_refresh(tokens.generate_refresh_token())
        assert excinfo.value.code == "refresh_token_invalid"

    def test_rotate_refresh_works_after_access_expired(self, env, clock):
        pair = env["service"].issue_pair("u1")
        clock.advance(ACCESS_TOKEN_TTL.total_seconds() + 60)
        new_pair = env["service"].rotate_refresh(pair.refresh_token)
        env["service"].validate_access(new_pair.access_token)

    def test_rotate_refresh_absolute_expiry_code(self, env, clock):
        pair = env["service"].issue_pair("u1")
        clock.advance(REFRESH_TOKEN_TTL.total_seconds() + 1)
        with pytest.raises(WebAuthError) as excinfo:
            env["service"].rotate_refresh(pair.refresh_token)
        assert excinfo.value.code == "refresh_token_expired"

    def test_rotate_refresh_inactive_user_revokes_family(self, env):
        pair = env["service"].issue_pair("u1")
        env["active"].discard("u1")
        with pytest.raises(WebAuthError) as excinfo:
            env["service"].rotate_refresh(pair.refresh_token)
        assert excinfo.value.code == "refresh_token_revoked"
        record = env["store"].get_session(pair.session_family_id)
        assert record.revoked_at is not None

    def test_logout_with_valid_access_revokes_family(self, env):
        pair = env["service"].issue_pair("u1")
        env["service"].logout(access_token=pair.access_token)
        with pytest.raises(WebAuthError):
            env["service"].validate_access(pair.access_token)
        with pytest.raises(WebAuthError) as excinfo:
            env["service"].rotate_refresh(pair.refresh_token)
        assert excinfo.value.code == "refresh_token_revoked"

    def test_logout_is_idempotent(self, env):
        pair = env["service"].issue_pair("u1")
        env["service"].logout(access_token=pair.access_token)
        env["service"].logout(access_token=pair.access_token)  # must not raise

    def test_logout_expired_access_requires_matching_refresh(self, env, clock):
        pair = env["service"].issue_pair("u1")
        clock.advance(ACCESS_TOKEN_TTL.total_seconds() + 60)

        with pytest.raises(WebAuthError) as excinfo:
            env["service"].logout(access_token=pair.access_token)
        assert excinfo.value.code == "access_token_expired"

        env["service"].logout(access_token=pair.access_token, refresh_token=pair.refresh_token)
        record = env["store"].get_session(pair.session_family_id)
        assert record.revoked_at is not None

    def test_logout_by_refresh_token_alone(self, env):
        pair = env["service"].issue_pair("u1")
        env["service"].logout(refresh_token=pair.refresh_token)
        with pytest.raises(WebAuthError):
            env["service"].validate_access(pair.access_token)

    def test_logout_with_nothing_usable_is_invalid(self, env):
        tokens = env["tokens"]
        with pytest.raises(WebAuthError) as excinfo:
            env["service"].logout(access_token=tokens.generate_access_token())
        assert excinfo.value.code == "access_token_invalid"
        with pytest.raises(WebAuthError) as excinfo:
            env["service"].logout(refresh_token=tokens.generate_refresh_token())
        assert excinfo.value.code == "refresh_token_invalid"

    def test_revoke_user_sessions_reports_count(self, env):
        env["service"].issue_pair("u1")
        env["service"].issue_pair("u1")
        assert env["service"].revoke_user_sessions("u1") == 2
        assert env["service"].revoke_user_sessions("u1") == 0

    def test_family_cap_revokes_oldest_beyond_limit(self, env, clock):
        assert MAX_ACTIVE_FAMILIES == 20
        families = []
        for _ in range(MAX_ACTIVE_FAMILIES):
            clock.advance(1)
            families.append(env["service"].issue_pair("u1").session_family_id)

        clock.advance(1)
        newest = env["service"].issue_pair("u1")

        assert env["store"].get_session(families[0]).revoked_at is not None
        assert env["store"].get_session(families[0]).revoke_reason == "family_cap"
        assert env["store"].get_session(families[1]).revoked_at is None
        assert env["store"].get_session(newest.session_family_id).revoked_at is None
        conn = sqlite3.connect(env["store"].db_path)
        try:
            active = conn.execute(
                "SELECT COUNT(*) FROM web_sessions "
                "WHERE user_id='u1' AND revoked_at IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        assert active == MAX_ACTIVE_FAMILIES


# ---------------------------------------------------------------------------
# Phase 4 support: bounded in-memory rate limiter for auth endpoints
# ---------------------------------------------------------------------------


class TestWebRateLimiter:
    def test_allows_up_to_limit_then_blocks(self):
        clock = FakeClock(start=EPOCH)
        limiter = WebRateLimiter(clock=clock)
        window = timedelta(minutes=1)
        assert [limiter.allow("login:ip:bob", 3, window) for _ in range(3)] == [True] * 3
        assert limiter.allow("login:ip:bob", 3, window) is False

    def test_window_expiry_releases_budget(self):
        clock = FakeClock(start=EPOCH)
        limiter = WebRateLimiter(clock=clock)
        window = timedelta(minutes=1)
        for _ in range(3):
            limiter.allow("k", 3, window)
        assert limiter.allow("k", 3, window) is False
        clock.advance(61)
        assert limiter.allow("k", 3, window) is True

    def test_keys_are_independent(self):
        clock = FakeClock(start=EPOCH)
        limiter = WebRateLimiter(clock=clock)
        window = timedelta(hours=1)
        for _ in range(3):
            limiter.allow("register:1.1.1.1:alice", 3, window)
        assert limiter.allow("register:1.1.1.1:alice", 3, window) is False
        assert limiter.allow("register:2.2.2.2:alice", 3, window) is True
        assert limiter.allow("register:1.1.1.1:bob", 3, window) is True
