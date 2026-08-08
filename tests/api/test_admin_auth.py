"""
Admin router auth-surface tests (auth-hardening task 1: /admin/health only;
key lifecycle coverage arrives with task 9).
"""

from memos.api.routers.admin_router import admin_health


def test_admin_health_defaults_to_auth_enabled(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.delenv("MASTER_KEY_HASH", raising=False)

    result = admin_health()

    assert result["status"] == "ok"
    assert result["auth_enabled"] is True
    assert result["master_key_configured"] is False


def test_admin_health_reflects_explicit_env(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("MASTER_KEY_HASH", "a" * 64)

    result = admin_health()

    assert result["auth_enabled"] is False
    assert result["master_key_configured"] is True
