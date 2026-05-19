"""Static tests for the PostgreSQL Docker Compose service template."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


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
    assert postgres["build"]["args"]["PG_SEARCH_VERSION"] == ("${GOBBY_PG_SEARCH_VERSION:-0.17.0}")
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

    assert "shared_preload_libraries=pg_search,pgaudit" in command
    assert "pgaudit.log=write" in command


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
