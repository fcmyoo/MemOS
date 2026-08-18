"""PostgreSQL connection helpers for the MemOS user backend.

This module owns only URL/engine construction; it carries no business
queries and never logs connection strings, passwords, or tokens.
"""

import os
import re

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, URL

_SCHEMA_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def build_postgres_url() -> URL:
    """Build the production PostgreSQL URL from the ``POSTGRES_*`` env vars.

    ``URL.create`` keeps password characters (``@``, ``:``, ...) from being
    mis-joined into the DSN; the object form is never stringified into logs.
    """
    return URL.create(
        drivername="postgresql+psycopg2",
        username=os.getenv("POSTGRES_USER", "memos"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        host=os.getenv("POSTGRES_HOST", "postgres"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.getenv("POSTGRES_DB", "memos"),
    )


def create_postgres_engine(
    database_url: str | URL | None = None,
    schema: str | None = None,
) -> Engine:
    """Create a SQLAlchemy engine, optionally pinning ``search_path`` to ``schema``.

    ``schema`` exists for test isolation only; production relies on the
    connection's default schema. The schema name is validated so it cannot
    smuggle SQL into the ``search_path`` connection option.
    """
    connect_args: dict[str, str] = {}
    if schema is not None:
        if _SCHEMA_PATTERN.fullmatch(schema) is None:
            raise ValueError("Invalid PostgreSQL schema name")
        connect_args["options"] = f"-csearch_path={schema}"
    return create_engine(
        database_url or build_postgres_url(),
        echo=False,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
