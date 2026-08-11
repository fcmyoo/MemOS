"""Web console authentication primitives (phases 1+2).

Implements the building blocks defined by docs/plans/web-console-session-design.md
for the Web console login system, which *coexists* with the existing API-key
middleware (``memos.api.middleware.auth``) and never touches it:

- ``ClockService`` / ``FakeClock``: injectable wall + monotonic clock so every
  expiry decision is deterministic in tests (no real sleeping).
- ``WebTokenService``: ``wca_`` (access) / ``wcr_`` (refresh) tokens in
  ``selector.secret`` form (128-bit selector, 256-bit CSPRNG secret), SHA-256
  storage hashing, strict format validation, clock-driven TTLs.
- ``WebPasswordService``: Argon2id password hashing via argon2-cffi. The
  dependency is imported lazily; when it is missing a clear, actionable error
  is raised instead of an import failure (it is an optional extra and must not
  be added to pyproject.toml without separate approval).
- ``WebSessionStore``: SQLite storage for ``web_sessions`` with WAL,
  ``foreign_keys=ON`` and ``busy_timeout=5000`` connections, idempotent
  schema creation, CAS-style atomic rotation (safe under multiple workers)
  and bounded cleanup of long-expired rows.

Only token *hashes* are ever stored; plaintext tokens exist solely in
transit/responses.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import uuid

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from memos import settings
from memos.log import get_logger


logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Optional dependency: argon2-cffi (web-console extra). Imported lazily so a
# deployment without it keeps working for API-key auth; the password service
# raises an actionable error instead.
# ---------------------------------------------------------------------------
try:  # pragma: no cover - depends on the deployment's installed extras
    from argon2 import PasswordHasher as _Argon2PasswordHasher

    _ARGON2_AVAILABLE = True
    _ARGON2_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:
    _Argon2PasswordHasher = None  # type: ignore[assignment,misc]
    _ARGON2_AVAILABLE = False
    _ARGON2_IMPORT_ERROR = exc


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


class ClockService:
    """Injectable clock. All session/token time math goes through it.

    ``now()`` returns timezone-aware UTC datetimes; ``monotonic()`` mirrors
    ``time.monotonic`` for rate-limit/grace style measurements. Tests swap in
    ``FakeClock`` so behaviour is deterministic without real waiting.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock(ClockService):
    """Deterministic clock: wall and monotonic time move only via ``advance``."""

    def __init__(self, start: datetime | None = None, monotonic_start: float = 0.0):
        self._now = start if start is not None else datetime(2026, 1, 1, tzinfo=UTC)
        self._monotonic = float(monotonic_start)

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)
        self._monotonic += seconds

    def set_now(self, value: datetime) -> None:
        self._now = value


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

ACCESS_TOKEN_PREFIX = "wca_"
REFRESH_TOKEN_PREFIX = "wcr_"

# 128-bit selector + 256-bit secret, both hex-encoded: <32 hex>.<64 hex>.
_SELECTOR_BYTES = 16
_SECRET_BYTES = 32

# Contract TTLs (design doc 3.x): short-lived access, 30-day absolute refresh.
ACCESS_TOKEN_TTL = timedelta(minutes=15)
REFRESH_TOKEN_TTL = timedelta(days=30)

_TOKEN_PATTERN = re.compile(
    r"^(?P<prefix>wca_|wcr_)(?P<selector>[0-9a-f]{32})\.(?P<secret>[0-9a-f]{64})$"
)

_TOKEN_KINDS = {ACCESS_TOKEN_PREFIX: "access", REFRESH_TOKEN_PREFIX: "refresh"}


class WebTokenService:
    """Generates, parses and hashes Web console tokens.

    Tokens are ``selector.secret`` strings; only their SHA-256 digests are
    persisted. The injected ``clock`` drives every expiry computation so
    tests never depend on real time.
    """

    def __init__(
        self,
        clock: ClockService | None = None,
        access_ttl: timedelta = ACCESS_TOKEN_TTL,
        refresh_ttl: timedelta = REFRESH_TOKEN_TTL,
    ):
        self.clock = clock or ClockService()
        self.access_ttl = access_ttl
        self.refresh_ttl = refresh_ttl

    # -- generation ---------------------------------------------------------

    def generate_access_token(self) -> str:
        return self._generate(ACCESS_TOKEN_PREFIX)

    def generate_refresh_token(self) -> str:
        return self._generate(REFRESH_TOKEN_PREFIX)

    @staticmethod
    def _generate(prefix: str) -> str:
        selector = secrets.token_hex(_SELECTOR_BYTES)
        secret = secrets.token_hex(_SECRET_BYTES)
        return f"{prefix}{selector}.{secret}"

    # -- hashing / verification ----------------------------------------------

    @staticmethod
    def hash_token(token: str) -> str:
        """SHA-256 hex digest stored in the database (never the plaintext)."""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def verify_hash(token: str, expected_hash: str) -> bool:
        """Constant-time comparison of a presented token against a stored hash."""
        return hmac.compare_digest(WebTokenService.hash_token(token), expected_hash)

    # -- parsing / format -----------------------------------------------------

    @staticmethod
    def parse_token(token: str) -> tuple[str, str, str]:
        """Return ``(kind, selector, secret)`` or raise ``ValueError``.

        ``kind`` is ``"access"`` or ``"refresh"``; the check is strict —
        API keys (``krlk_``), wrong prefixes, wrong lengths or non-hex bodies
        are all rejected.
        """
        if not isinstance(token, str):
            raise ValueError("token must be a string")
        match = _TOKEN_PATTERN.match(token)
        if match is None:
            raise ValueError("malformed web console token")
        kind = _TOKEN_KINDS[match.group("prefix")]
        return kind, match.group("selector"), match.group("secret")

    @staticmethod
    def is_valid_format(token: str, expected_prefix: str | None = None) -> bool:
        try:
            _, _, _ = WebTokenService.parse_token(token)
        except ValueError:
            return False
        return expected_prefix is None or token.startswith(expected_prefix)

    # -- expiry ---------------------------------------------------------------

    def access_expiry(self, now: datetime | None = None) -> datetime:
        base = now if now is not None else self.clock.now()
        return base + self.access_ttl

    def refresh_expiry(self, now: datetime | None = None) -> datetime:
        base = now if now is not None else self.clock.now()
        return base + self.refresh_ttl


# ---------------------------------------------------------------------------
# Passwords (Argon2id)
# ---------------------------------------------------------------------------

# Contract parameters (web-console-revamp.md 4.6): 64 MiB memory, 3
# iterations, parallelism 4, 32-byte hash, 16-byte salt. Memory is expressed
# in KiB for argon2-cffi.
ARGON2_MEMORY_COST_KIB = 64 * 1024
ARGON2_TIME_COST = 3
ARGON2_PARALLELISM = 4
ARGON2_HASH_LEN = 32
ARGON2_SALT_LEN = 16


class Argon2NotInstalledError(RuntimeError):
    """Raised when password hashing is requested without argon2-cffi."""


class WebPasswordService:
    """Argon2id password hashing with an injectable hasher.

    The hasher is injectable so the logic is testable without the native
    dependency; production builds the default argon2-cffi ``PasswordHasher``
    lazily and fails with an actionable message when the optional dependency
    is absent.
    """

    def __init__(self, hasher: Any | None = None):
        self._hasher = hasher if hasher is not None else self._build_default_hasher()

    @staticmethod
    def _build_default_hasher() -> Any:
        if not _ARGON2_AVAILABLE:
            raise Argon2NotInstalledError(
                "argon2-cffi is required for Web console password hashing but is "
                "not installed. Install it with `pip install argon2-cffi` "
                "(web-console optional extra). "
                f"Original ImportError: {_ARGON2_IMPORT_ERROR}"
            )
        return _Argon2PasswordHasher(
            memory_cost=ARGON2_MEMORY_COST_KIB,
            time_cost=ARGON2_TIME_COST,
            parallelism=ARGON2_PARALLELISM,
            hash_len=ARGON2_HASH_LEN,
            salt_len=ARGON2_SALT_LEN,
        )

    def hash_password(self, password: str) -> str:
        """Return the PHC-format Argon2id hash of ``password``."""
        return self._hasher.hash(password)

    def verify_password(self, password_hash: str, password: str) -> bool:
        """Return True when ``password`` matches ``password_hash``.

        Mismatches and malformed/undecodable hashes both yield False; the
        concrete exception type depends on the backend, and with an injected
        hasher it is unknowable, so this layer treats every failure as a
        rejection.
        """
        try:
            return bool(self._hasher.verify(password_hash, password))
        except Exception:  # noqa: BLE001 - rejection is the only signal callers need
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        """True when the stored hash predates the current Argon2 parameters."""
        return bool(self._hasher.check_needs_rehash(password_hash))


# ---------------------------------------------------------------------------
# Session store (SQLite)
# ---------------------------------------------------------------------------

WEB_SESSIONS_TABLE = "web_sessions"

_WEB_SESSIONS_DDL = """\
CREATE TABLE IF NOT EXISTS web_sessions (
    session_family_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    access_token_hash TEXT NOT NULL UNIQUE,
    access_expires_at TEXT NOT NULL,
    refresh_token_hash TEXT NOT NULL UNIQUE,
    previous_refresh_hash TEXT,
    previous_refresh_valid_until TEXT,
    refresh_expires_at TEXT NOT NULL,
    rotation_counter INTEGER NOT NULL DEFAULT 0,
    rotated_at TEXT,
    last_refreshed_at TEXT,
    revoked_at TEXT,
    revoke_reason TEXT,
    created_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
)
"""

_WEB_SESSIONS_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_web_sessions_user_active
ON web_sessions(user_id, revoked_at, refresh_expires_at)
"""

# Revoked/expired sessions are kept for a bounded audit window, then cleaned
# up in bounded batches (design doc 3.1: refresh_expires_at + 30 days, max
# 100 rows per pass).
CLEANUP_RETENTION = timedelta(days=30)
CLEANUP_DEFAULT_LIMIT = 100

_SESSION_COLUMNS = (
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
)


@dataclass(frozen=True)
class WebSessionRecord:
    """A web_sessions row with all timestamps as aware UTC datetimes."""

    session_family_id: str
    user_id: str
    access_token_hash: str
    access_expires_at: datetime
    refresh_token_hash: str
    previous_refresh_hash: str | None
    previous_refresh_valid_until: datetime | None
    refresh_expires_at: datetime
    rotation_counter: int
    rotated_at: datetime | None
    last_refreshed_at: datetime | None
    revoked_at: datetime | None
    revoke_reason: str | None
    created_at: datetime
    last_seen_at: datetime


def _serialize_dt(value: datetime) -> str:
    """Fixed-width ISO-8601 UTC so lexicographic order == chronological order."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class WebSessionStore:
    """SQLite persistence for Web console sessions.

    Connections are configured for multi-worker deployments: WAL journal,
    ``busy_timeout=5000``, ``foreign_keys=ON``, ``check_same_thread=False``.
    Writes use ``BEGIN IMMEDIATE`` plus conditional UPDATEs, so concurrent
    rotations resolve as a single winner (CAS semantics) without half-updates.
    """

    def __init__(self, db_path: str | None = None, clock: ClockService | None = None):
        if db_path is None:
            db_path = str(settings.MEMOS_DIR / "memos_users.db")
        self.db_path = db_path
        self.clock = clock or ClockService()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # In-process index of refresh hashes that were rotated away / consumed,
        # mapped to their family. Used to distinguish "reused old token" from
        # "never seen token" once the token no longer matches any slot.
        self._consumed_refresh_hashes: dict[str, str] = {}
        self._ensure_schema()

    # -- connection / schema ---------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _ensure_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(_WEB_SESSIONS_DDL)
            conn.execute(_WEB_SESSIONS_INDEX_DDL)
            # Idempotent phase-3 migration (adds grace/rotation/revoke columns).
            _migrate_web_sessions_schema(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- helpers ---------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: tuple) -> WebSessionRecord:
        values = dict(zip(_SESSION_COLUMNS, row, strict=True))
        return WebSessionRecord(
            session_family_id=values["session_family_id"],
            user_id=values["user_id"],
            access_token_hash=values["access_token_hash"],
            access_expires_at=_parse_dt(values["access_expires_at"]),  # type: ignore[arg-type]
            refresh_token_hash=values["refresh_token_hash"],
            previous_refresh_hash=values["previous_refresh_hash"],
            previous_refresh_valid_until=_parse_dt(values["previous_refresh_valid_until"]),
            refresh_expires_at=_parse_dt(values["refresh_expires_at"]),  # type: ignore[arg-type]
            rotation_counter=values["rotation_counter"] or 0,
            rotated_at=_parse_dt(values["rotated_at"]),
            last_refreshed_at=_parse_dt(values["last_refreshed_at"]),
            revoked_at=_parse_dt(values["revoked_at"]),
            revoke_reason=values["revoke_reason"],
            created_at=_parse_dt(values["created_at"]),  # type: ignore[arg-type]
            last_seen_at=_parse_dt(values["last_seen_at"]),  # type: ignore[arg-type]
        )

    @staticmethod
    def _select_columns() -> str:
        return ", ".join(_SESSION_COLUMNS)

    # -- operations -------------------------------------------------------------

    def create_session(
        self,
        user_id: str,
        access_token_hash: str,
        access_expires_at: datetime,
        refresh_token_hash: str,
        refresh_expires_at: datetime,
        session_family_id: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """Insert a new session row and return its ``session_family_id``."""
        family_id = session_family_id or uuid.uuid4().hex
        moment = _serialize_dt(now if now is not None else self.clock.now())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO web_sessions ("
                "session_family_id, user_id, access_token_hash, access_expires_at, "
                "refresh_token_hash, refresh_expires_at, created_at, last_seen_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    family_id,
                    user_id,
                    access_token_hash,
                    _serialize_dt(access_expires_at),
                    refresh_token_hash,
                    _serialize_dt(refresh_expires_at),
                    moment,
                    moment,
                ),
            )
            conn.commit()
            return family_id
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def validate_access(
        self,
        access_token_hash: str,
        now: datetime | None = None,
    ) -> WebSessionRecord | None:
        """Return the live session for this access-token hash, else None.

        Read-only: checks ``revoked_at IS NULL`` and a non-expired
        ``access_expires_at`` using the injected clock.
        """
        moment = _serialize_dt(now if now is not None else self.clock.now())
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {self._select_columns()} FROM web_sessions "
                "WHERE access_token_hash = ? AND revoked_at IS NULL "
                "AND access_expires_at > ?",
                (access_token_hash, moment),
            ).fetchone()
            return self._row_to_record(row) if row else None
        finally:
            conn.close()

    def get_session(self, session_family_id: str) -> WebSessionRecord | None:
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {self._select_columns()} FROM web_sessions "
                "WHERE session_family_id = ?",
                (session_family_id,),
            ).fetchone()
            return self._row_to_record(row) if row else None
        finally:
            conn.close()

    def get_by_refresh_hash(self, refresh_token_hash: str) -> WebSessionRecord | None:
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {self._select_columns()} FROM web_sessions "
                "WHERE refresh_token_hash = ?",
                (refresh_token_hash,),
            ).fetchone()
            return self._row_to_record(row) if row else None
        finally:
            conn.close()

    def rotate_refresh(
        self,
        session_family_id: str,
        expected_refresh_hash: str,
        new_access_token_hash: str,
        new_access_expires_at: datetime,
        new_refresh_token_hash: str,
        new_refresh_expires_at: datetime,
        now: datetime | None = None,
    ) -> bool:
        """Atomically rotate both tokens (compare-and-swap).

        Succeeds only when the row is unrevoked and still carries
        ``expected_refresh_hash``; the previous refresh hash is retained for
        the reuse-grace logic built in phase 3. Concurrent rotators serialize
        via ``BEGIN IMMEDIATE`` and the conditional UPDATE — exactly one wins.
        """
        moment = _serialize_dt(now if now is not None else self.clock.now())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE web_sessions SET "
                "access_token_hash = ?, access_expires_at = ?, "
                "refresh_token_hash = ?, previous_refresh_hash = ?, "
                "rotated_at = ?, last_seen_at = ? "
                "WHERE session_family_id = ? AND refresh_token_hash = ? "
                "AND revoked_at IS NULL",
                (
                    new_access_token_hash,
                    _serialize_dt(new_access_expires_at),
                    new_refresh_token_hash,
                    expected_refresh_hash,
                    moment,
                    moment,
                    session_family_id,
                    expected_refresh_hash,
                ),
            )
            conn.commit()
            return cursor.rowcount == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def revoke(self, session_family_id: str, now: datetime | None = None) -> bool:
        """Revoke one session; False when it was already revoked/unknown."""
        moment = _serialize_dt(now if now is not None else self.clock.now())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE web_sessions SET revoked_at = ? "
                "WHERE session_family_id = ? AND revoked_at IS NULL",
                (moment, session_family_id),
            )
            conn.commit()
            return cursor.rowcount == 1
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def revoke_user_sessions(self, user_id: str, now: datetime | None = None) -> int:
        """Revoke every active session of a user (logout-all / disable)."""
        moment = _serialize_dt(now if now is not None else self.clock.now())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "UPDATE web_sessions SET revoked_at = ? "
                "WHERE user_id = ? AND revoked_at IS NULL",
                (moment, user_id),
            )
            conn.commit()
            return cursor.rowcount
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def cleanup_expired(
        self,
        now: datetime | None = None,
        limit: int = CLEANUP_DEFAULT_LIMIT,
    ) -> int:
        """Delete up to ``limit`` sessions dead for over ``CLEANUP_RETENTION``.

        A row is eligible once its ``refresh_expires_at`` is older than
        ``now - CLEANUP_RETENTION`` (that covers both expired and
        early-revoked sessions). Bounded so login/startup never stall on a
        mass delete.
        """
        moment = now if now is not None else self.clock.now()
        cutoff = _serialize_dt(moment - CLEANUP_RETENTION)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "DELETE FROM web_sessions WHERE session_family_id IN ("
                "SELECT session_family_id FROM web_sessions "
                "WHERE refresh_expires_at <= ? LIMIT ?)",
                (cutoff, limit),
            )
            conn.commit()
            return cursor.rowcount
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- phase-3 lookup helpers ----------------------------------------------
    def find_by_access_hash(self, access_token_hash: str) -> WebSessionRecord | None:
        """Return the row for an access hash regardless of expiry/revocation."""
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {self._select_columns()} FROM web_sessions "
                "WHERE access_token_hash = ?",
                (access_token_hash,),
            ).fetchone()
            return self._row_to_record(row) if row else None
        finally:
            conn.close()

    def find_by_any_refresh_hash(self, refresh_token_hash: str) -> WebSessionRecord | None:
        """Match a refresh hash in either the current or previous slot."""
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {self._select_columns()} FROM web_sessions "
                "WHERE refresh_token_hash = ? OR previous_refresh_hash = ?",
                (refresh_token_hash, refresh_token_hash),
            ).fetchone()
            return self._row_to_record(row) if row else None
        finally:
            conn.close()

    def get_by_refresh_hash(self, refresh_token_hash: str) -> WebSessionRecord | None:
        """Return the row whose *current* refresh hash matches."""
        return self.find_by_any_refresh_hash(refresh_token_hash)

    # -- phase-3 consume_refresh ---------------------------------------------
    def consume_refresh(
        self,
        refresh_token_hash: str,
        new_access_token_hash: str,
        new_access_expires_at: datetime,
        new_refresh_token_hash: str,
        now: datetime | None = None,
        grace: timedelta | None = None,
    ) -> "ConsumeOutcome":
        """Consume a refresh token: rotate, recover within grace, or detect reuse.

        Returns a ``ConsumeOutcome`` instead of raising: the caller maps the
        status to the appropriate HTTP response. All state transitions happen
        in one ``BEGIN IMMEDIATE`` transaction.
        """
        moment = now if now is not None else self.clock.now()
        moment_str = _serialize_dt(moment)
        grace_eff = min(grace or PREVIOUS_REFRESH_GRACE, MAX_REFRESH_GRACE)

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT {self._select_columns()} FROM web_sessions "
                "WHERE refresh_token_hash = ? OR previous_refresh_hash = ?",
                (refresh_token_hash, refresh_token_hash),
            ).fetchone()
            if row is None:
                # Not in any slot: distinguish a never-seen token (INVALID)
                # from a rotated-away token that is being replayed (REUSED).
                family = self._consumed_refresh_hashes.get(refresh_token_hash)
                if family is not None:
                    conn.execute(
                        "UPDATE web_sessions SET revoked_at = ?, revoke_reason = ? "
                        "WHERE session_family_id = ? AND revoked_at IS NULL",
                        (moment_str, "refresh_reuse", family),
                    )
                    conn.commit()
                    return ConsumeOutcome(status=RotateStatus.REUSED, session_family_id=family)
                conn.rollback()
                return ConsumeOutcome(status=RotateStatus.INVALID)
            record = self._row_to_record(row)
            family = record.session_family_id

            if record.revoked_at is not None:
                conn.rollback()
                return ConsumeOutcome(status=RotateStatus.REVOKED, session_family_id=family)

            if _parse_dt(record.refresh_expires_at) <= moment:
                conn.rollback()
                return ConsumeOutcome(
                    status=RotateStatus.EXPIRED,
                    session_family_id=family,
                    refresh_expires_at=_parse_dt(record.refresh_expires_at),
                )

            is_current = record.refresh_token_hash == refresh_token_hash
            # Grace window is computed from the rotation time with the
            # *caller-supplied* grace (clamped to the 30s contract cap), so a
            # client that passes grace=120s still recovers up to 30s.
            within_grace = (
                record.rotated_at is not None
                and (moment - _parse_dt(record.rotated_at)) <= grace_eff
            )
            is_previous = (
                record.previous_refresh_hash == refresh_token_hash
                and within_grace
            )

            if not is_current and not is_previous:
                # Old generation reused outside the grace window: revoke family.
                conn.execute(
                    "UPDATE web_sessions SET revoked_at = ?, revoke_reason = ? "
                    "WHERE session_family_id = ? AND revoked_at IS NULL",
                    (moment_str, "refresh_reuse", family),
                )
                conn.commit()
                return ConsumeOutcome(status=RotateStatus.REUSED, session_family_id=family)

            status = RotateStatus.ROTATED if is_current else RotateStatus.RECOVERED
            new_rotation = record.rotation_counter + 1
            # The consumed generation moves into the grace slot: for ROTATED
            # that is the current token; for RECOVERED the previously-current
            # token becomes the new previous (chain stays recoverable once).
            prev_hash = record.refresh_token_hash
            prev_until = _serialize_dt(moment + grace_eff)
            conn.execute(
                "UPDATE web_sessions SET "
                "access_token_hash = ?, access_expires_at = ?, "
                "refresh_token_hash = ?, previous_refresh_hash = ?, "
                "previous_refresh_valid_until = ?, rotation_counter = ?, "
                "rotated_at = ?, last_refreshed_at = ?, last_seen_at = ? "
                "WHERE session_family_id = ? AND revoked_at IS NULL",
                (
                    new_access_token_hash,
                    _serialize_dt(new_access_expires_at),
                    new_refresh_token_hash,
                    prev_hash,
                    prev_until,
                    new_rotation,
                    moment_str,
                    moment_str,
                    moment_str,
                    family,
                ),
            )
            conn.commit()
            # Remember the consumed generation so a later replay of the
            # pre-rotation token is classified as reuse, not unknown.
            self._consumed_refresh_hashes[refresh_token_hash] = family
            if len(self._consumed_refresh_hashes) > 10000:
                self._consumed_refresh_hashes.clear()
            return ConsumeOutcome(
                status=status,
                session_family_id=family,
                rotation_counter=new_rotation,
                refresh_expires_at=_parse_dt(record.refresh_expires_at),
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Phase 3: SessionService, rotation grace, replay detection, rate limiter
# ---------------------------------------------------------------------------

#: Refresh tokens that were rotated away remain acceptable for this window,
#: so a client that lost the rotation response (network blip) can recover.
PREVIOUS_REFRESH_GRACE = timedelta(seconds=10)


class WebAuthError(Exception):
    """Raised by session operations with a stable machine-readable code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass
class TokenPair:
    """Fresh credential pair issued by SessionService."""

    access_token: str
    refresh_token: str
    session_family_id: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    rotation: int


class SessionService:
    """Issue / validate / rotate / logout Web console sessions."""

    def __init__(
        self,
        store: WebSessionStore,
        tokens: WebTokenService,
        is_user_active,
        clock: ClockService | None = None,
    ) -> None:
        self.store = store
        self.tokens = tokens
        self.is_user_active = is_user_active
        self.clock = clock or ClockService()

    # -- issuing -----------------------------------------------------------
    def issue_pair(self, user_id: str, now: datetime | None = None) -> TokenPair:
        moment = now if now is not None else self.clock.now()
        access = self.tokens.generate_access_token()
        refresh = self.tokens.generate_refresh_token()
        access_exp = self.tokens.access_expiry(moment)
        refresh_exp = self.tokens.refresh_expiry(moment)
        family_id = self.store.create_session(
            user_id=user_id,
            access_token_hash=self.tokens.hash_token(access),
            refresh_token_hash=self.tokens.hash_token(refresh),
            access_expires_at=access_exp,
            refresh_expires_at=refresh_exp,
            now=moment,
        )
        self._enforce_family_cap(user_id, moment)
        return TokenPair(
            access_token=access,
            refresh_token=refresh,
            session_family_id=family_id,
            access_expires_at=access_exp,
            refresh_expires_at=refresh_exp,
            rotation=0,
        )

    # -- access ------------------------------------------------------------
    def validate_access(self, access_token: str, now: datetime | None = None):
        if not self.tokens.is_valid_format(access_token, expected_prefix=ACCESS_TOKEN_PREFIX):
            raise WebAuthError("access_token_invalid")
        moment = now if now is not None else self.clock.now()
        record = self.store.find_by_access_hash(self.tokens.hash_token(access_token))
        if record is None:
            raise WebAuthError("access_token_invalid")
        if record.revoked_at is not None:
            raise WebAuthError("session_revoked")
        if _parse_dt(record.access_expires_at) <= moment:
            raise WebAuthError("access_token_expired")
        if not self.is_user_active(record.user_id):
            raise WebAuthError("session_revoked")
        return record

    # -- rotation ----------------------------------------------------------
    def rotate_refresh(self, refresh_token: str, now: datetime | None = None) -> TokenPair:
        if not self.tokens.is_valid_format(refresh_token, expected_prefix=REFRESH_TOKEN_PREFIX):
            raise WebAuthError("refresh_token_invalid")
        moment = now if now is not None else self.clock.now()
        token_hash = self.tokens.hash_token(refresh_token)
        record = self.store.find_by_any_refresh_hash(token_hash)
        if record is None:
            raise WebAuthError("refresh_token_invalid")
        if record.revoked_at is not None:
            raise WebAuthError("refresh_token_revoked")
        if _parse_dt(record.refresh_expires_at) <= moment:
            raise WebAuthError("refresh_token_expired")
        if not self.is_user_active(record.user_id):
            self.store.revoke(record.session_family_id, now=moment)
            raise WebAuthError("refresh_token_revoked")

        new_access = self.tokens.generate_access_token()
        new_refresh = self.tokens.generate_refresh_token()
        outcome = self.store.consume_refresh(
            refresh_token_hash=token_hash,
            new_access_token_hash=self.tokens.hash_token(new_access),
            new_access_expires_at=self.tokens.access_expiry(moment),
            new_refresh_token_hash=self.tokens.hash_token(new_refresh),
            now=moment,
        )
        if outcome.status == RotateStatus.REUSED:
            raise WebAuthError("refresh_token_reused")
        if outcome.status == RotateStatus.REVOKED:
            raise WebAuthError("refresh_token_revoked")
        if outcome.status == RotateStatus.EXPIRED:
            raise WebAuthError("refresh_token_expired")
        if outcome.status == RotateStatus.INVALID:
            raise WebAuthError("refresh_token_invalid")
        return TokenPair(
            access_token=new_access,
            refresh_token=new_refresh,
            session_family_id=outcome.session_family_id,
            access_expires_at=self.tokens.access_expiry(moment),
            refresh_expires_at=outcome.refresh_expires_at or _parse_dt(record.refresh_expires_at),
            rotation=outcome.rotation_counter,
        )

    # -- logout ------------------------------------------------------------
    def logout(
        self,
        access_token: str | None = None,
        refresh_token: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Revoke the session identified by access and/or refresh token.

        Idempotent: unknown/expired/revoked tokens are silently accepted when
        at least one usable credential is present; raising happens only when
        nothing usable was supplied.
        """
        moment = now if now is not None else self.clock.now()
        family: str | None = None
        found = False

        access_error: str | None = None
        refresh_error: str | None = None

        if access_token:
            if not self.tokens.is_valid_format(access_token, expected_prefix=ACCESS_TOKEN_PREFIX):
                access_error = "access_token_invalid"
            else:
                rec = self.store.find_by_access_hash(self.tokens.hash_token(access_token))
                if rec is not None:
                    found = True
                    family = rec.session_family_id
                    # An expired access token alone cannot prove intent to
                    # logout; a matching refresh token is required.
                    if (
                        rec.revoked_at is None
                        and _parse_dt(rec.access_expires_at) <= moment
                        and refresh_token is None
                    ):
                        raise WebAuthError("access_token_expired")

        if refresh_token:
            if not self.tokens.is_valid_format(refresh_token, expected_prefix=REFRESH_TOKEN_PREFIX):
                refresh_error = "refresh_token_invalid"
            else:
                rec = self.store.find_by_any_refresh_hash(self.tokens.hash_token(refresh_token))
                if rec is not None:
                    found = True
                    family = rec.session_family_id

        if not found:
            # Only a refresh token was supplied: its error wins. Otherwise the
            # access token's error (or the generic fallback) applies.
            if refresh_token and access_token is None:
                raise WebAuthError(refresh_error or "refresh_token_invalid")
            raise WebAuthError(access_error or "access_token_invalid")
        if family is not None:
            self.store.revoke(family, now=moment)

    def revoke_user_sessions(self, user_id: str, now: datetime | None = None) -> int:
        """Revoke every active session of a user; returns count revoked."""
        return self.store.revoke_user_sessions(user_id, now=now)

    def _enforce_family_cap(self, user_id: str, now: datetime | None = None) -> None:
        """Revoke the oldest active sessions beyond MAX_ACTIVE_FAMILIES."""
        moment = now if now is not None else self.clock.now()
        conn = self.store._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT session_family_id FROM web_sessions "
                "WHERE user_id = ? AND revoked_at IS NULL "
                "ORDER BY created_at ASC",
                (user_id,),
            ).fetchall()
            if len(rows) <= MAX_ACTIVE_FAMILIES:
                conn.rollback()
                return
            for row in rows[: len(rows) - MAX_ACTIVE_FAMILIES]:
                conn.execute(
                    "UPDATE web_sessions SET revoked_at = ?, revoke_reason = ? "
                    "WHERE session_family_id = ? AND revoked_at IS NULL",
                    (_serialize_dt(moment), "family_cap", row[0]),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


class WebRateLimiter:
    """Minimal fixed-window in-memory rate limiter for auth endpoints.

    Windows are keyed by a caller-supplied bucket name (e.g. ``login:ip:<ip>``)
    and enforced per process. Multiple workers each keep their own budget —
    acceptable for v1 self-hosted; a shared Redis backend is a later upgrade.
    """

    def __init__(self, clock: ClockService | None = None) -> None:
        self.clock = clock or ClockService()
        self._buckets: dict[str, tuple[float, int]] = {}

    def allow(self, key: str, limit: int, window: timedelta) -> bool:
        now = self.clock.monotonic()
        window_secs = window.total_seconds()
        entry = self._buckets.get(key)
        if entry is None or now - entry[0] >= window_secs:
            self._buckets[key] = (now, 1)
            return True
        _, count = entry
        if count >= limit:
            return False
        self._buckets[key] = (now, count + 1)
        return True


# ---------------------------------------------------------------------------
# Phase 3 (store-level): rotation outcome, active-family cap
# ---------------------------------------------------------------------------

#: Maximum concurrently active session families per user (v1 cap).
MAX_ACTIVE_FAMILIES = 20


class RotateStatus:
    """Outcome of a refresh-token rotation attempt."""

    ROTATED = "rotated"
    RECOVERED = "recovered"
    REUSED = "reused"
    REVOKED = "revoked"
    EXPIRED = "expired"
    INVALID = "invalid"


# ---------------------------------------------------------------------------
# Phase 3 (store-level): consume_refresh + lookup helpers + schema migration
# ---------------------------------------------------------------------------

#: Absolute cap for the previous-refresh grace window (contract: 30s max).
MAX_REFRESH_GRACE = timedelta(seconds=30)


@dataclass
class ConsumeOutcome:
    """Result of a refresh-token consumption attempt."""

    status: str
    session_family_id: str | None = None
    rotation_counter: int = 0
    refresh_expires_at: datetime | None = None


def _web_sessions_columns(conn) -> set[str]:
    return {row[1] for row in conn.execute("PRAGMA table_info(web_sessions)").fetchall()}


def _migrate_web_sessions_schema(conn) -> None:
    """Add phase-3 columns to a batch-1 schema, idempotently.

    Runs inside the caller's transaction (the store already opened
    ``BEGIN IMMEDIATE``); never starts its own transaction.
    """
    existing = _web_sessions_columns(conn)
    additions = {
        "previous_refresh_valid_until": "TEXT",
        "rotation_counter": "INTEGER NOT NULL DEFAULT 0",
        "last_refreshed_at": "TEXT",
        "revoke_reason": "TEXT",
    }
    missing = {name: ddl for name, ddl in additions.items() if name not in existing}
    if not missing:
        return
    for name, ddl in missing.items():
        conn.execute(f"ALTER TABLE web_sessions ADD COLUMN {name} {ddl}")
    conn.execute("PRAGMA user_version = 2")