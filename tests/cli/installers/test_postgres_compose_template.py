"""Static tests for the PostgreSQL Docker Compose service template."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_PGAUDIT_COMMAND_OPTIONS = [
    "shared_preload_libraries=pg_search,pgaudit",
    "pgaudit.log=${GOBBY_PGAUDIT_LOG:-ddl}",
    "pgaudit.log_catalog=off",
    "logging_collector=on",
    "log_destination=stderr",
    "log_directory=/var/log/pgaudit",
    "log_filename=pgaudit-%Y-%m-%d_%H%M%S.log",
    "log_rotation_age=1d",
    "log_rotation_size=0",
    "log_file_mode=0640",
    "log_min_messages=log",
]


@pytest.fixture
def compose_data(repo_root: Path) -> dict[str, object]:
    compose_path = repo_root / "src/gobby/data/docker-compose.services.yml"
    data = yaml.safe_load(compose_path.read_text())
    assert isinstance(data, dict)
    return data


def test_compose_defines_postgres_alongside_existing_services(
    compose_data: dict[str, object],
) -> None:
    services = compose_data["services"]

    assert "neo4j" in services
    assert "qdrant" in services
    assert "postgres" in services


def test_postgres_service_uses_local_build_context_and_tag(
    compose_data: dict[str, object],
) -> None:
    postgres = compose_data["services"]["postgres"]

    assert postgres["image"] == "gobby-postgres-local:17-pgsearch"
    assert postgres["container_name"] == "gobby-postgres"
    assert postgres["build"]["context"] == "./postgres-pgsearch"
    assert postgres["build"]["args"]["PG_SEARCH_VERSION"] == ("${GOBBY_PG_SEARCH_VERSION:-0.23.4}")
    assert postgres["build"]["args"]["PG_SEARCH_SHA256"] == "${GOBBY_PG_SEARCH_SHA256}"


def test_postgres_service_has_required_profiles_ports_and_volumes(
    compose_data: dict[str, object],
) -> None:
    postgres = compose_data["services"]["postgres"]

    assert set(postgres["profiles"]) == {"postgres", "all"}
    assert "${GOBBY_POSTGRES_PORT:-60891}:5432" in postgres["ports"]
    assert "gobby_postgres_data:/var/lib/postgresql/data" in postgres["volumes"]
    assert "gobby_pgaudit_log:/var/log/pgaudit" in postgres["volumes"]
    assert "gobby_postgres_data" in compose_data["volumes"]
    assert "gobby_pgaudit_log" in compose_data["volumes"]


def test_postgres_service_preloads_pg_search_and_pgaudit(
    compose_data: dict[str, object],
) -> None:
    command = " ".join(compose_data["services"]["postgres"]["command"])

    for option in _PGAUDIT_COMMAND_OPTIONS:
        assert option in command


def test_postgres_service_healthcheck_probes_validation_window_audit_capture(
    compose_data: dict[str, object],
) -> None:
    healthcheck = compose_data["services"]["postgres"]["healthcheck"]
    test_command = " ".join(str(part) for part in healthcheck["test"])

    assert "pg_isready" in test_command
    assert "pg_extension" in test_command
    assert "extname='pgaudit'" in test_command
    assert "extname=$pgaudit$" not in test_command
    assert "SHOW pgaudit.log" in test_command
    assert "GOBBY_PGAUDIT_LOG:-ddl" in test_command
    assert "/var/log/pgaudit" in test_command
    assert "pgaudit-*.log" in test_command
    assert "stat -c '%U %a'" in test_command
    assert "postgres 640" in test_command
    assert "UPDATE _pgaudit_probe SET last_probed_at = NOW()" in test_command
    assert "AUDIT: SESSION" in test_command
    assert "UPDATE" in test_command


def test_postgres_service_has_pg_isready_healthcheck(
    compose_data: dict[str, object],
) -> None:
    healthcheck = compose_data["services"]["postgres"]["healthcheck"]
    test_command = " ".join(str(part) for part in healthcheck["test"])

    assert "pg_isready" in test_command
    assert "${GOBBY_POSTGRES_USER:-gobby}" in test_command
    assert healthcheck["interval"] == "5s"
    assert healthcheck["timeout"] == "3s"
    assert healthcheck["retries"] == 10
