"""Tests for API key authentication middleware."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from fastapi import HTTPException

from memos.api.middleware import auth


@pytest.fixture
def request_mock() -> Mock:
    """Create a reusable mock request object."""
    return Mock(headers={}, client=SimpleNamespace(host="8.8.8.8"))


@pytest.mark.asyncio
async def test_auth_disabled_bypasses_check(monkeypatch: pytest.MonkeyPatch, request_mock: Mock):
    """Auth disabled should bypass key verification and return bypass metadata."""
    monkeypatch.setattr(auth, "AUTH_ENABLED", False)

    result = await auth.verify_api_key(request=request_mock, api_key=None)

    assert result["auth_bypassed"] is True
    assert result["scopes"] == ["all"]


@pytest.mark.asyncio
async def test_missing_api_key_returns_401(monkeypatch: pytest.MonkeyPatch, request_mock: Mock):
    """When auth is enabled and key is missing, middleware should return 401."""
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "MASTER_KEY_HASH", None)

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_api_key(request=request_mock, api_key=None)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_master_key_authentication(monkeypatch: pytest.MonkeyPatch, request_mock: Mock):
    """Valid master key should authenticate as admin with all scopes."""
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    master_key = "mk_test_master_key"
    monkeypatch.setattr(auth, "MASTER_KEY_HASH", auth.hash_api_key(master_key))

    result = await auth.verify_api_key(request=request_mock, api_key=master_key)

    assert result["is_master_key"] is True
    assert result["scopes"] == ["all"]
    assert result["user_name"] == "admin"


@pytest.mark.asyncio
async def test_invalid_key_format_returns_401(monkeypatch: pytest.MonkeyPatch, request_mock: Mock):
    """Invalid regular key format should fail with 401."""
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "MASTER_KEY_HASH", None)

    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_api_key(request=request_mock, api_key="invalid_key")

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_valid_key_lookup_success(monkeypatch: pytest.MonkeyPatch, request_mock: Mock):
    """Valid formatted key with successful lookup should return user and scopes."""
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "MASTER_KEY_HASH", None)
    monkeypatch.setattr(
        auth,
        "lookup_api_key",
        AsyncMock(return_value={"id": "k1", "user_name": "alice", "scopes": ["read"]}),
    )

    valid_key = "krlk_" + "a" * 64
    result = await auth.verify_api_key(request=request_mock, api_key=valid_key)

    assert result["user_name"] == "alice"
    assert result["scopes"] == ["read"]
    assert result["api_key_id"] == "k1"


@pytest.mark.asyncio
async def test_expired_key_returns_none(monkeypatch: pytest.MonkeyPatch, request_mock: Mock):
    """Lookup returning None should be treated as invalid/expired key and return 401."""
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "MASTER_KEY_HASH", None)
    monkeypatch.setattr(auth, "lookup_api_key", AsyncMock(return_value=None))

    valid_key = "krlk_" + "b" * 64
    with pytest.raises(HTTPException) as exc_info:
        await auth.verify_api_key(request=request_mock, api_key=valid_key)

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_require_scope_read_allows_read():
    """Read scope checker should pass when auth context includes read scope."""
    checker = auth.require_scope("read")

    result = await checker(auth={"scopes": ["read"], "user_name": "u1"})

    assert result["user_name"] == "u1"


@pytest.mark.asyncio
async def test_require_scope_write_denies_read_only():
    """Write scope checker should reject auth context that only has read scope."""
    checker = auth.require_scope("write")

    with pytest.raises(HTTPException) as exc_info:
        await checker(auth={"scopes": ["read"], "user_name": "u1"})

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_scope_all_grants_everything():
    """All scope should satisfy any required scope including admin."""
    checker = auth.require_scope("admin")

    result = await checker(auth={"scopes": ["all"], "user_name": "u1"})

    assert result["user_name"] == "u1"


@pytest.mark.asyncio
async def test_internal_request_bypass(monkeypatch: pytest.MonkeyPatch):
    """Internal request source should bypass API key requirement."""
    monkeypatch.setattr(auth, "AUTH_ENABLED", True)
    monkeypatch.setattr(auth, "MASTER_KEY_HASH", None)
    request = Mock(headers={}, client=SimpleNamespace(host="127.0.0.1"))

    result = await auth.verify_api_key(request=request, api_key=None)

    assert result["is_internal"] is True
    assert result["scopes"] == ["all"]
