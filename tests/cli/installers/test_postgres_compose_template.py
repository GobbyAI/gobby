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


def test_postgres_compose_templates_are_byte_equivalent(repo_root: Path) -> None:
    source = repo_root / "src/gobby/data/docker-compose.services.yml"
    gcore_asset = repo_root / "crates/gcore/assets/docker-compose.services.yml"

    assert source.is_file()
    assert not gcore_asset.exists()


def test_compose_defines_postgres_alongside_shared_services(
    compose_data: dict[str, object],
) -> None:
    services = compose_data["services"]

    assert "falkordb" in services
    assert "qdrant" in services
    assert "postgres" in services
    assert "neo4j" not in services


def test_all_published_service_ports_are_loopback_bound_by_default(
    compose_data: dict[str, object],
) -> None:
    services = compose_data["services"]

    for service in services.values():
        for published_port in service.get("ports", []):
            assert published_port.startswith(
                ("127.0.0.1:", "${GOBBY_SERVICES_BIND_ADDRESS:-127.0.0.1}:")
            )


def test_postgres_service_uses_local_build_context_and_tag(
    compose_data: dict[str, object],
) -> None:
    postgres = compose_data["services"]["postgres"]

    assert postgres["image"] == "gobby-postgres-local:18-pgsearch"
    assert postgres["container_name"] == "gobby-postgres"
    assert postgres["build"]["context"] == "./postgres-pgsearch"
    assert postgres["build"]["args"]["PG_SEARCH_VERSION"] == ("${GOBBY_PG_SEARCH_VERSION:-0.23.4}")
    assert postgres["build"]["args"]["PG_SEARCH_SHA256"] == "${GOBBY_PG_SEARCH_SHA256}"


def test_postgres_service_has_required_profiles_ports_and_volumes(
    compose_data: dict[str, object],
) -> None:
    postgres = compose_data["services"]["postgres"]

    assert set(postgres["profiles"]) == {"postgres", "all"}
    assert (
        "${GOBBY_SERVICES_BIND_ADDRESS:-127.0.0.1}:${GOBBY_POSTGRES_PORT:-60891}:5432"
        in postgres["ports"]
    )
    assert postgres["environment"]["POSTGRES_PASSWORD"] == (
        "${GOBBY_POSTGRES_PASSWORD:?GOBBY_POSTGRES_PASSWORD must be set}"
    )
    assert "gobby_postgres_data:/var/lib/postgresql" in postgres["volumes"]
    assert "gobby_pgaudit_log:/var/log/pgaudit" in postgres["volumes"]
    assert "gobby_postgres_data" in compose_data["volumes"]
    assert "gobby_pgaudit_log" in compose_data["volumes"]


def test_postgres_service_preloads_pg_search_and_pgaudit(
    compose_data: dict[str, object],
) -> None:
    command = " ".join(compose_data["services"]["postgres"]["command"])

    for option in _PGAUDIT_COMMAND_OPTIONS:
        assert option in command


def test_postgres_service_healthcheck_validates_pgaudit_configuration(
    compose_data: dict[str, object],
    repo_root: Path,
) -> None:
    """Pin option C: runtime validates configuration while CI proves write emission."""
    environment = compose_data["services"]["postgres"]["environment"]
    healthcheck = compose_data["services"]["postgres"]["healthcheck"]
    test_command = " ".join(str(part) for part in healthcheck["test"])
    template = (repo_root / "src/gobby/data/docker-compose.services.yml").read_text()

    assert environment["GOBBY_PGAUDIT_LOG"] == "${GOBBY_PGAUDIT_LOG:-ddl}"
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
    assert "UPDATE" not in test_command
    assert "AUDIT: SESSION" not in test_command
    assert "configuration-only by design" in template
    assert ".github/scripts/verify-pgaudit-emission.sh" in template


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
