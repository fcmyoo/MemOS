"""Shared API test configuration.

Module-level environment setup runs during pytest collection, before any
test module imports ``memos.api.server_api`` / ``memos.api.routers.server_router``.
This guarantees ``chat_handler`` is instantiated uniformly in every test file
(module-level ``chat_handler`` in server_router is None unless
ENABLE_CHAT_API=true) — without it, the first file that imports the router
would cache ``chat_handler=None`` and later files would get 503 on chat
endpoints.

``memos/api/config.py`` calls ``load_dotenv(override=True)``, which would
silently clobber these values from ``.env`` the moment the API import chain
runs. We neutralize only the ``override`` semantics for the whole session
(keeping the real loader otherwise intact so the load_dotenv-order unit
tests in test_auth.py keep passing): process-env values then always win, and
each test file controls its own env explicitly (MEMOS_BASE_PATH,
AUTH_ENABLED, ... are set on the command line or patched per-test).
"""
import os
import re
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import dotenv
import psycopg2
import pytest
from psycopg2 import sql
from sqlalchemy.engine import URL

os.environ["ENABLE_CHAT_API"] = "true"

_real_load_dotenv = dotenv.load_dotenv


def _load_dotenv_without_override(*args, **kwargs):
    """Real load_dotenv, but never override existing process env vars."""
    kwargs.pop("override", None)
    return _real_load_dotenv(*args, **kwargs)


# Applies for the whole test session; per-test monkeypatching of
# dotenv.load_dotenv (test_auth.py) stacks on top of this cleanly.
patch("dotenv.load_dotenv", _load_dotenv_without_override).start()

# If a test file imported the API entry chain before this conftest ran, the
# cached modules were built with the old env value. Drop them so the next
# ``from memos.api import server_api`` re-imports with ENABLE_CHAT_API=true.
for _name in (
    "memos.api.server_api",
    "memos.api.server_api_ext",
    "memos.api.routers.server_router",
):
    sys.modules.pop(_name, None)


_SCHEMA_PATTERN = re.compile(r"^[a-z0-9_]+$")
_REQUIRED_ENV = ("HOST", "PORT", "USER", "PASSWORD", "DB")


@pytest.fixture
def postgres_test_schema():
    """Create an isolated ``memos_test_<uuid>`` schema; yield ``url``/``schema``/``register``.

    Mirrors ``tests/mem_user/conftest.py``: only ``MEMOS_TEST_POSTGRES_*`` are
    read (missing → skip), never the production ``POSTGRES_*`` values. Teardown
    disposes every registered SQLAlchemy engine before ``DROP SCHEMA ... CASCADE``.
    """
    missing = [
        name for name in _REQUIRED_ENV if not os.getenv(f"MEMOS_TEST_POSTGRES_{name}")
    ]
    if missing:
        pytest.skip(
            "PostgreSQL integration tests not enabled: missing "
            + ", ".join(f"MEMOS_TEST_POSTGRES_{name}" for name in missing)
            + " (set MEMOS_TEST_POSTGRES_*; POSTGRES_* is never used)"
        )

    cfg = {
        "host": os.environ["MEMOS_TEST_POSTGRES_HOST"],
        "port": int(os.environ["MEMOS_TEST_POSTGRES_PORT"]),
        "user": os.environ["MEMOS_TEST_POSTGRES_USER"],
        "password": os.environ["MEMOS_TEST_POSTGRES_PASSWORD"],
        "database": os.environ["MEMOS_TEST_POSTGRES_DB"],
    }

    schema = f"memos_test_{uuid.uuid4().hex}"
    assert _SCHEMA_PATTERN.fullmatch(schema), f"invalid test schema name: {schema}"

    url = URL.create(
        drivername="postgresql+psycopg2",
        username=cfg["user"],
        password=cfg["password"],
        host=cfg["host"],
        port=cfg["port"],
        database=cfg["database"],
    )

    admin_conn = psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        dbname=cfg["database"],
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
