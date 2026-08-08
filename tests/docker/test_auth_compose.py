"""
Static validation of ``docker/docker-compose.yml`` auth hardening.

Checks run against the file text only — no Docker daemon and no real
database are involved. When PyYAML happens to be installed, an extra
structural pass validates the parsed document; the text assertions do not
depend on it so no new project dependency is introduced.

Run ``docker compose --env-file .env -f docker/docker-compose.yml config``
on a host with Docker to complement these checks.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = REPO_ROOT / "docker" / "docker-compose.yml"
INIT_SQL_PATH = REPO_ROOT / "docker" / "postgres" / "init" / "001_api_keys.sql"

try:
    import yaml
except ImportError:  # PyYAML is not a project dependency; degrade gracefully
    yaml = None

requires_yaml = pytest.mark.skipif(yaml is None, reason="PyYAML not installed")

STORAGE_SERVICES = ("neo4j", "qdrant", "postgres")

EXPECTED_MEMOS_ENV = [
    "AUTH_ENABLED=${AUTH_ENABLED:-true}",
    "MASTER_KEY_HASH=${MASTER_KEY_HASH:?MASTER_KEY_HASH must be set}",
    "INTERNAL_SERVICE_SECRET=${INTERNAL_SERVICE_SECRET:?INTERNAL_SERVICE_SECRET must be set}",
    "POSTGRES_HOST=postgres",
    "POSTGRES_PORT=5432",
    "POSTGRES_DB=${POSTGRES_DB:-memos}",
    "POSTGRES_USER=${POSTGRES_USER:-memos}",
    "POSTGRES_PASSWORD=${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}",
]

EXPECTED_BINDINGS = {
    "memos": ['"${MEMOS_BIND_ADDRESS:-127.0.0.1}:8000:8000"'],
    "neo4j": [
        '"${NEO4J_BIND_ADDRESS:-127.0.0.1}:7474:7474"',
        '"${NEO4J_BIND_ADDRESS:-127.0.0.1}:7687:7687"',
    ],
    "qdrant": [
        '"${QDRANT_BIND_ADDRESS:-127.0.0.1}:6333:6333"',
        '"${QDRANT_BIND_ADDRESS:-127.0.0.1}:6334:6334"',
    ],
    "postgres": ['"${POSTGRES_BIND_ADDRESS:-127.0.0.1}:5432:5432"'],
}


@pytest.fixture(scope="module")
def compose_text() -> str:
    return COMPOSE_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def service_blocks(compose_text: str) -> dict[str, str]:
    """Slice the compose text into per-service blocks (2-space indent)."""
    blocks: dict[str, str] = {}
    matches = list(re.finditer(r"\n  ([\w-]+):\n", compose_text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(compose_text)
        blocks[match.group(1)] = compose_text[match.end() : end]
    return blocks


def test_no_hardcoded_password_anywhere(compose_text: str):
    assert "12345678" not in compose_text


def test_init_sql_script_exists():
    assert INIT_SQL_PATH.is_file(), f"missing {INIT_SQL_PATH}"


def test_no_bare_host_container_port_mappings(compose_text: str):
    """Every published port must carry an explicit host bind address."""
    bare_ports = re.findall(r'^\s*-\s*"?(\d{2,5}:\d{2,5})"?\s*(?:#.*)?$', compose_text, re.MULTILINE)
    assert not bare_ports, f"bare HOST:CONTAINER port mappings leaked: {bare_ports}"


@pytest.mark.parametrize("service", EXPECTED_BINDINGS)
def test_default_bind_is_loopback(compose_text: str, service: str):
    for binding in EXPECTED_BINDINGS[service]:
        assert binding in compose_text, f"{service} missing loopback binding {binding}"


def test_required_interpolation_for_secrets(compose_text: str):
    assert "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}" in compose_text
    assert "${NEO4J_PASSWORD:?NEO4J_PASSWORD must be set}" in compose_text
    assert "${MASTER_KEY_HASH:?MASTER_KEY_HASH must be set}" in compose_text
    assert "${INTERNAL_SERVICE_SECRET:?INTERNAL_SERVICE_SECRET must be set}" in compose_text
    # No weak fallback defaults for secrets.
    assert not re.search(r"\$\{(POSTGRES_PASSWORD|NEO4J_PASSWORD|MASTER_KEY_HASH|INTERNAL_SERVICE_SECRET):-", compose_text)


def test_memos_receives_auth_and_postgres_env(service_blocks: dict[str, str]):
    memos = service_blocks["memos"]
    for entry in EXPECTED_MEMOS_ENV:
        assert entry in memos, f"memos service missing environment entry: {entry}"


def test_init_sql_mounted_readonly_into_entrypoint_dir(service_blocks: dict[str, str]):
    postgres = service_blocks["postgres"]
    assert (
        "./postgres/init/001_api_keys.sql:/docker-entrypoint-initdb.d/001_api_keys.sql:ro"
        in postgres
    )


def test_postgres_service_shape(service_blocks: dict[str, str]):
    postgres = service_blocks["postgres"]
    assert "image: postgres:16-alpine" in postgres
    assert "pg_isready" in postgres, "postgres healthcheck must use pg_isready"
    assert "postgres_data:/var/lib/postgresql/data" in postgres


def test_memos_waits_for_healthy_postgres(service_blocks: dict[str, str]):
    memos = service_blocks["memos"]
    depends = re.search(r"depends_on:\n((?:\s+.+\n)+)", memos)
    assert depends, "memos depends_on block not found"
    block = depends.group(1)
    assert re.search(r"postgres:\n\s+condition: service_healthy", block)
    assert re.search(r"neo4j:\n\s+condition: service_healthy", block)
    assert re.search(r"qdrant:\n\s+condition: service_started", block)
    # Legacy list form must be gone.
    assert not re.search(r"depends_on:\n\s+- ", block)


def test_postgres_data_volume_declared(compose_text: str):
    top_level_volumes = compose_text[compose_text.rindex("\nvolumes:") :]
    assert re.search(r"^  postgres_data:\s*$", top_level_volumes, re.MULTILINE)


@requires_yaml
def test_compose_is_valid_yaml(compose_text: str):
    document = yaml.safe_load(compose_text)
    services = document["services"]
    assert set(STORAGE_SERVICES) <= set(services)
    memos_env = services["memos"]["environment"]
    assert "AUTH_ENABLED=${AUTH_ENABLED:-true}" in memos_env
    postgres_mounts = services["postgres"]["volumes"]
    assert any(str(mount).endswith("/docker-entrypoint-initdb.d/001_api_keys.sql:ro") for mount in postgres_mounts)
    assert services["memos"]["depends_on"]["postgres"]["condition"] == "service_healthy"
