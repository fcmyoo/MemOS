"""
Static validation of the api_keys PostgreSQL schema script.

This module parses ``docker/postgres/init/001_api_keys.sql`` as text and
asserts the column definitions, constraints, and idempotency markers that
the authentication layer relies on. It never connects to a real database;
a Docker-backed integration run (execute the script twice, then exercise
``create_api_key_in_db`` / ``lookup_api_key`` / ``list_api_keys`` /
``revoke_api_key``) belongs in CI when a PostgreSQL container is available.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SQL_PATH = REPO_ROOT / "docker" / "postgres" / "init" / "001_api_keys.sql"

RUNTIME_COLUMNS = [
    "id",
    "key_hash",
    "key_prefix",
    "user_name",
    "scopes",
    "description",
    "expires_at",
    "is_active",
    "last_used_at",
    "created_at",
    "created_by",
]


@pytest.fixture(scope="module")
def sql_text() -> str:
    return SQL_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def table_body(sql_text: str) -> str:
    """Return the text between CREATE TABLE ... ( and its closing );"""
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS api_keys\s*\((.*)\)\s*;",
        sql_text,
        re.DOTALL | re.IGNORECASE,
    )
    assert match, "CREATE TABLE IF NOT EXISTS api_keys ... ; not found"
    return match.group(1)


def _column_line(table_body: str, column: str) -> str:
    for line in table_body.splitlines():
        stripped = line.strip()
        if re.match(rf"^{column}\s+", stripped):
            return stripped
    raise AssertionError(f"column {column!r} not found in api_keys definition")


def test_sql_file_exists():
    assert SQL_PATH.is_file(), f"missing schema script at {SQL_PATH}"


@pytest.mark.parametrize("column", RUNTIME_COLUMNS)
def test_runtime_columns_present(table_body: str, column: str):
    assert _column_line(table_body, column)


def test_key_hash_is_unique_sha256_column(table_body: str):
    line = _column_line(table_body, "key_hash")
    assert "VARCHAR(64)" in line.upper()
    assert "NOT NULL" in line.upper()
    assert "UNIQUE" in line.upper()
    assert re.search(
        r"CONSTRAINT api_keys_key_hash_sha256 CHECK \(key_hash ~ '\^\[0-9a-f\]\{64\}\$'\)",
        table_body,
    ), "key_hash must be constrained to lowercase SHA-256 hex"


def test_key_prefix_format_constraint(table_body: str):
    line = _column_line(table_body, "key_prefix")
    assert "VARCHAR(12)" in line.upper()
    assert re.search(
        r"CONSTRAINT api_keys_key_prefix_format CHECK \(key_prefix ~ '\^krlk_\[0-9a-f\]\{7\}\$'\)",
        table_body,
    )


def test_scopes_is_text_array_default_read(table_body: str):
    line = _column_line(table_body, "scopes")
    assert re.search(r"TEXT\[\]", line, re.IGNORECASE), "scopes must be TEXT[]"
    assert "NOT NULL" in line.upper()
    assert re.search(r"DEFAULT ARRAY\['read'\]::TEXT\[\]", line, re.IGNORECASE)


def test_timestamp_columns_are_timestamptz(table_body: str):
    for column in ("expires_at", "last_used_at", "created_at"):
        line = _column_line(table_body, column)
        assert "TIMESTAMPTZ" in line.upper(), f"{column} must be TIMESTAMPTZ"


def test_created_at_defaults_to_now(table_body: str):
    line = _column_line(table_body, "created_at")
    assert "NOT NULL" in line.upper()
    assert re.search(r"DEFAULT NOW\(\)", line, re.IGNORECASE)


def test_keys_active_by_default(table_body: str):
    line = _column_line(table_body, "is_active")
    assert "BOOLEAN" in line.upper()
    assert "NOT NULL" in line.upper()
    assert re.search(r"DEFAULT TRUE", line, re.IGNORECASE)


def test_scopes_nonempty_constraint(table_body: str):
    assert re.search(
        r"CONSTRAINT api_keys_scopes_nonempty CHECK \(cardinality\(scopes\) > 0\)",
        table_body,
    )


def test_primary_key_uses_builtin_gen_random_uuid(table_body: str):
    line = _column_line(table_body, "id")
    assert "UUID" in line.upper()
    assert "PRIMARY KEY" in line.upper()
    assert re.search(r"DEFAULT gen_random_uuid\(\)", line, re.IGNORECASE)
    # postgres:16-alpine provides gen_random_uuid() built in; the legacy
    # extension must not be required.
    assert "uuid-ossp" not in line.lower()


def test_script_is_idempotent(sql_text: str):
    """Running the script twice on the same database must not fail."""
    assert re.search(r"CREATE TABLE IF NOT EXISTS api_keys", sql_text)
    index_statements = re.findall(r"CREATE INDEX[^\n]*", sql_text)
    assert index_statements, "expected index creation statements"
    for statement in index_statements:
        assert "IF NOT EXISTS" in statement


def test_expected_indexes_present(sql_text: str):
    assert re.search(
        r"CREATE INDEX IF NOT EXISTS idx_api_keys_user_created\s+"
        r"ON api_keys \(user_name, created_at DESC\)",
        sql_text,
        re.IGNORECASE,
    )
    assert re.search(
        r"CREATE INDEX IF NOT EXISTS idx_api_keys_expiration\s+ON api_keys \(expires_at\)\s+"
        r"WHERE is_active = TRUE AND expires_at IS NOT NULL",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    )
    # key_hash UNIQUE already creates an index; no duplicate hash index.
    assert "idx_api_keys_key_hash" not in sql_text
