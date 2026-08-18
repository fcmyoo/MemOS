"""PostgreSQL integration fixtures for the mem_user contract tests.

These fixtures drive the PostgreSQL backend against a real, isolated schema.
They only read the ``MEMOS_TEST_POSTGRES_*`` environment variables and never
fall back to the production ``POSTGRES_*`` values, so a run without an
explicitly provisioned test database skips instead of touching ``memos``.
"""

import os
import re
import uuid

from types import SimpleNamespace

import psycopg2
import pytest
from psycopg2 import sql
from sqlalchemy.engine import URL


_SCHEMA_PATTERN = re.compile(r"^[a-z0-9_]+$")

_REQUIRED_ENV = ("HOST", "PORT", "USER", "PASSWORD", "DB")


def _test_postgres_settings():
    """Return the ``MEMOS_TEST_POSTGRES_*`` connection settings, or skip.

    ``POSTGRES_*`` is deliberately never consulted: the fixture fails closed
    (skips) rather than connect to the default production database.
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
    return {
        "host": os.environ["MEMOS_TEST_POSTGRES_HOST"],
        "port": int(os.environ["MEMOS_TEST_POSTGRES_PORT"]),
        "user": os.environ["MEMOS_TEST_POSTGRES_USER"],
        "password": os.environ["MEMOS_TEST_POSTGRES_PASSWORD"],
        "database": os.environ["MEMOS_TEST_POSTGRES_DB"],
    }


@pytest.fixture
def postgres_test_schema():
    """Create an isolated ``memos_test_<uuid>`` schema; yield ``url`` + ``schema``.

    Teardown disposes every SQLAlchemy engine the test registered via
    ``register(engine)`` and then drops the schema with ``DROP SCHEMA ...
    CASCADE`` using a quoted ``sql.Identifier`` (never string concatenation).
    """
    cfg = _test_postgres_settings()
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
        """Register an engine so teardown disposes it before dropping the schema."""
        engines.append(engine)

    yield SimpleNamespace(url=url, schema=schema, register=register)

    for engine in engines:
        engine.dispose()
    with admin_conn.cursor() as cur:
        cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
    admin_conn.close()
