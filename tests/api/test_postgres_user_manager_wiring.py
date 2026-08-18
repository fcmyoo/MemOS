"""Wiring contract tests for the PostgreSQL backend switch (T3 + T5).

Verifies that the three production instantiation sites —
``server_api.py`` (lifespan), ``server_router.py`` (module-level singleton) and
``cube_handler.py`` — obtain their manager through the runtime factory
``create_runtime_user_manager`` rather than constructing a SQLite
``UserManager`` directly, and that ``WebSessionStore()`` defaults to a
PostgreSQL engine without creating ``memos_users.db``.

NOTE: these tests deliberately avoid re-importing ``server_api`` /
``server_router`` / ``cube_handler`` modules. Re-importing them with mocked
components replaces the module objects in ``sys.modules``, which poisons later
test files (e.g. ``test_server_router.py``) whose fixtures re-import
``server_api`` and pick up the mock-wired singletons → 403s (object-identity
loss on ``dependency_overrides`` keys). Source inspection asserts the wiring
contract without any module reload.
"""

import inspect
import sys

from unittest.mock import Mock, patch

import pytest

from memos.mem_user import factory as factory_module
from memos.mem_user.postgres_user_manager import PostgresUserManager

_MANAGER_SITES = (
    "memos.api.handlers.cube_handler",
    "memos.api.routers.server_router",
    "memos.api.server_api",
)


def test_production_sites_use_runtime_factory(monkeypatch, tmp_path):
    """All three production instantiation sites call the runtime factory.

    The factory is the ONLY place that reads ``USER_DB_BACKEND`` and picks the
    backend; a hardcoded ``UserManager()`` anywhere would bypass the switch.
    """
    for module_name in _MANAGER_SITES:
        module = __import__(module_name, fromlist=["*"])
        source = inspect.getsource(module)
        assert "create_runtime_user_manager" in source, (
            f"{module_name} must obtain its manager via the runtime factory"
        )
        assert "UserManager()" not in source, (
            f"{module_name} must not construct UserManager() directly"
        )


def test_factory_defaults_to_postgres_and_supports_sqlite_fallback(monkeypatch):
    """USER_DB_BACKEND defaults to postgres; explicit sqlite stays supported."""
    monkeypatch.delenv("USER_DB_BACKEND", raising=False)
    mgr = factory_module.create_runtime_user_manager()
    assert isinstance(mgr, PostgresUserManager)
    mgr.close()

    monkeypatch.setenv("USER_DB_BACKEND", "sqlite")
    from memos.mem_user.user_manager import UserManager

    mgr2 = factory_module.create_runtime_user_manager()
    assert isinstance(mgr2, UserManager)
    mgr2.close()


def test_factory_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("USER_DB_BACKEND", "oracle")
    with pytest.raises(ValueError):
        factory_module.create_runtime_user_manager()


def test_web_session_store_defaults_to_postgres_engine(monkeypatch, tmp_path):
    from memos import settings as settings_module
    from memos.api import web_auth

    sentinel = Mock(name="postgres_engine")
    monkeypatch.setattr(settings_module, "MEMOS_DIR", tmp_path)
    monkeypatch.setattr(
        web_auth, "create_postgres_engine", lambda *args, **kwargs: sentinel, raising=False
    )
    # Schema DDL is covered by test_postgres_web_session_store.py; here we only
    # assert the constructor wires the default PostgreSQL engine.
    monkeypatch.setattr(web_auth.WebSessionStore, "_ensure_schema", lambda self: None)

    store = web_auth.WebSessionStore()

    assert getattr(store, "engine", None) is sentinel
    assert not (tmp_path / "memos_users.db").exists()


def test_no_memos_users_db_is_created(monkeypatch, tmp_path):
    """The PG switch must never fall back to creating the SQLite file."""
    from memos import settings as settings_module

    monkeypatch.setattr(settings_module, "MEMOS_DIR", tmp_path)
    monkeypatch.setenv("USER_DB_BACKEND", "postgres")
    mgr = factory_module.create_runtime_user_manager()
    assert not (tmp_path / "memos_users.db").exists()
    mgr.close()
