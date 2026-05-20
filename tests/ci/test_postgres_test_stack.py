"""Static contract tests for the PostgreSQL test stack and CI wiring."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit


def test_test_compose_defines_ephemeral_postgres_test_service(repo_root: Path) -> None:
    compose_path = repo_root / "docker-compose.test.yml"

    assert compose_path.is_file(), "docker-compose.test.yml must define postgres-test"

    compose = _load_yaml(compose_path)
    services = _mapping(compose["services"])
    postgres = _mapping(services["postgres-test"])
    manifest = _load_pg_search_manifest(repo_root)
    build = _mapping(postgres["build"])
    build_args = _mapping(build["args"])

    assert postgres["image"] == "gobby-postgres-local:17-pgsearch"
    assert "gobby/postgres" not in str(postgres)
    assert build["context"] == "./src/gobby/data/postgres-pgsearch"
    assert build_args["PG_SEARCH_VERSION"] == (
        f"${{GOBBY_PG_SEARCH_VERSION:-{manifest['pg_search_version']}}}"
    )
    assert build_args["PG_SEARCH_SHA256"] == "${GOBBY_PG_SEARCH_SHA256}"
    assert postgres["command"] == [
        "postgres",
        "-c",
        "shared_preload_libraries=pg_search,pgaudit",
        "-c",
        "pgaudit.log=write",
    ]
    assert _mapping(postgres["environment"]) == {
        "POSTGRES_DB": "gobby_test",
        "POSTGRES_USER": "gobby_test",
        "POSTGRES_PASSWORD": "gobby_test",
    }
    assert postgres["ports"] == ["60892:5432"]
    assert postgres["tmpfs"] == ["/var/lib/postgresql/data"]

    healthcheck = _mapping(postgres["healthcheck"])
    assert healthcheck["test"] == ["CMD-SHELL", "pg_isready -U gobby_test"]
    assert healthcheck["interval"] == "2s"
    assert healthcheck["timeout"] == "2s"
    assert healthcheck["retries"] == 15


def test_ci_test_job_builds_and_runs_local_postgres_test_container(repo_root: Path) -> None:
    workflow = _load_yaml(repo_root / ".github/workflows/ci.yml")
    test_job = _mapping(_mapping(workflow["jobs"])["test"])
    runs = _step_runs(_sequence(test_job["steps"]))
    env = _mapping(test_job.get("env", {}))

    assert "services" not in test_job, "CI must local-build Postgres instead of pulling a service"
    assert env.get("DATABASE_URL") == (
        "postgresql://gobby_test:gobby_test@localhost:60892/gobby_test"
    )
    assert _has_run(
        runs,
        "jq -r '.pg_search_version' src/gobby/data/postgres-pgsearch/version.json",
        "jq -r '.pg_search_sha256' src/gobby/data/postgres-pgsearch/version.json",
        "GITHUB_ENV",
    )
    assert _has_run(
        runs,
        "docker build",
        '--build-arg PG_SEARCH_VERSION="${GOBBY_PG_SEARCH_VERSION}"',
        '--build-arg PG_SEARCH_SHA256="${GOBBY_PG_SEARCH_SHA256}"',
        "-t gobby-postgres-local:17-pgsearch",
        "src/gobby/data/postgres-pgsearch",
    )
    assert _has_run(
        runs,
        "docker run -d",
        "--name postgres-test",
        "-e POSTGRES_DB=gobby_test",
        "-e POSTGRES_USER=gobby_test",
        "-e POSTGRES_PASSWORD=gobby_test",
        "-p 60892:5432",
        "--tmpfs /var/lib/postgresql/data",
        "gobby-postgres-local:17-pgsearch",
        "postgres -c shared_preload_libraries=pg_search,pgaudit -c pgaudit.log=write",
    )
    assert _has_run(
        runs,
        "docker inspect",
        "postgres-test",
        "healthy",
    )
    assert _has_run(runs, "uv run pytest")


def _load_yaml(path: Path) -> Mapping[str, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


def _load_pg_search_manifest(repo_root: Path) -> Mapping[str, Any]:
    data = json.loads((repo_root / "src/gobby/data/postgres-pgsearch/version.json").read_text())
    assert isinstance(data, dict)
    return data


def _mapping(value: object) -> Mapping[str, Any]:
    assert isinstance(value, dict)
    return value


def _sequence(value: object) -> Sequence[object]:
    assert isinstance(value, list)
    return value


def _step_runs(steps: Sequence[object]) -> list[str]:
    runs: list[str] = []
    for step in steps:
        step_mapping = _mapping(step)
        run = step_mapping.get("run")
        if isinstance(run, str):
            runs.append(run)
    return runs


def _has_run(runs: Sequence[str], *needles: str) -> bool:
    return any(all(needle in run for needle in needles) for run in runs)
