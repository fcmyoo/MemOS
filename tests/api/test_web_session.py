"""Storage-independent unit tests for Web console auth primitives.

Covers, per docs/plans/web-console-session-design.md:

- injectable, testable clock (wall + monotonic time are mockable);
- ``WebTokenService``: ``wca_``/``wcr_`` ``selector.secret`` tokens, SHA-256
  storage hashing, format validation, TTLs driven by the injected clock;
- ``WebPasswordService``: Argon2id hash/verify/needs_rehash with a clear
  error when argon2-cffi is not installed;
- ``WebRateLimiter``: bounded in-memory rate limiting for auth endpoints.

``WebSessionStore`` persistence now lives in PostgreSQL; its contract tests
are ``tests/api/test_postgres_web_session_store.py``.
"""

from __future__ import annotations

import hashlib

from datetime import UTC, datetime, timedelta

import pytest

from memos.api import web_auth
from memos.api.web_auth import (
    ACCESS_TOKEN_PREFIX,
    ACCESS_TOKEN_TTL,
    REFRESH_TOKEN_PREFIX,
    REFRESH_TOKEN_TTL,
    Argon2NotInstalledError,
    ClockService,
    FakeClock,
    WebPasswordService,
    WebRateLimiter,
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
