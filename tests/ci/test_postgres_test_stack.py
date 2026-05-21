"""Static contract tests for the PostgreSQL test stack and CI wiring."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_PG_SEARCH_VERSION_ENV = "GOBBY_PG_SEARCH_VERSION"
_PG_SEARCH_SHA256_ENV = "GOBBY_PG_SEARCH_SHA256"

_POSTGRES_TEST_CONTAINER_ENV = "GOBBY_POSTGRES_TEST_CONTAINER"
_POSTGRES_TEST_DB_ENV = "GOBBY_POSTGRES_TEST_DB"
_POSTGRES_TEST_IMAGE_ENV = "GOBBY_POSTGRES_TEST_IMAGE"
_POSTGRES_TEST_PASSWORD_ENV = "GOBBY_POSTGRES_TEST_PASSWORD"
_POSTGRES_TEST_PORT_ENV = "GOBBY_POSTGRES_TEST_PORT"
_POSTGRES_TEST_USER_ENV = "GOBBY_POSTGRES_TEST_USER"

_POSTGRES_TEST_CONTAINER = "postgres-test"
_POSTGRES_TEST_DB = "gobby_test"
_POSTGRES_TEST_IMAGE = "gobby-postgres-local:18-pgsearch"
_POSTGRES_TEST_PASSWORD = "gobby_test"
_POSTGRES_TEST_PORT = "60892"
_POSTGRES_TEST_USER = "gobby_test"

_PGAUDIT_COMMAND_OPTIONS = [
    "shared_preload_libraries=pg_search,pgaudit",
    "pgaudit.log=write",
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

_POSTGRES_COMMAND = [
    "postgres",
    *(option for command_option in _PGAUDIT_COMMAND_OPTIONS for option in ("-c", command_option)),
]


def test_test_compose_defines_ephemeral_postgres_test_service(repo_root: Path) -> None:
    compose_path = repo_root / "docker-compose.test.yml"

    assert compose_path.is_file(), "docker-compose.test.yml must define postgres-test"

    compose = _load_yaml(compose_path)
    services = _mapping(compose["services"])
    postgres = _mapping(services["postgres-test"])
    manifest = _load_pg_search_manifest(repo_root)
    build = _mapping(postgres["build"])
    build_args = _mapping(build["args"])

    assert postgres["image"] == _POSTGRES_TEST_IMAGE
    assert "gobby/postgres" not in str(postgres)
    assert build["context"] == "./src/gobby/data/postgres-pgsearch"
    assert build_args["PG_SEARCH_VERSION"] == _compose_default(
        _PG_SEARCH_VERSION_ENV,
        str(manifest["pg_search_version"]),
    )
    assert build_args["PG_SEARCH_SHA256"] == _compose_default(
        _PG_SEARCH_SHA256_ENV,
        str(manifest["pg_search_sha256"]),
    )
    assert postgres["command"] == _POSTGRES_COMMAND
    assert _mapping(postgres["environment"]) == {
        "POSTGRES_DB": _compose_default(_POSTGRES_TEST_DB_ENV, _POSTGRES_TEST_DB),
        "POSTGRES_USER": _compose_default(_POSTGRES_TEST_USER_ENV, _POSTGRES_TEST_USER),
        "POSTGRES_PASSWORD": _compose_default(
            _POSTGRES_TEST_PASSWORD_ENV,
            _POSTGRES_TEST_PASSWORD,
        ),
    }
    assert postgres["ports"] == [
        f"{_compose_default(_POSTGRES_TEST_PORT_ENV, _POSTGRES_TEST_PORT)}:5432"
    ]
    assert postgres["tmpfs"] == ["/var/lib/postgresql"]

    healthcheck = _mapping(postgres["healthcheck"])
    assert healthcheck["test"] == [
        "CMD-SHELL",
        f"pg_isready -U {_compose_default(_POSTGRES_TEST_USER_ENV, _POSTGRES_TEST_USER)}",
    ]
    assert healthcheck["interval"] == "2s"
    assert healthcheck["timeout"] == "2s"
    assert healthcheck["retries"] == 15


def test_ci_test_job_builds_and_runs_local_postgres_test_container(repo_root: Path) -> None:
    workflow = _load_yaml(repo_root / ".github/workflows/ci.yml")
    test_job = _mapping(_mapping(workflow["jobs"])["test"])
    runs = _step_runs(_sequence(test_job["steps"]))
    env = _mapping(test_job.get("env", {}))

    assert "services" not in test_job, "CI must local-build Postgres instead of pulling a service"
    assert "DATABASE_URL" not in env
    assert _job_env(env) == {
        _POSTGRES_TEST_CONTAINER_ENV: _POSTGRES_TEST_CONTAINER,
        _POSTGRES_TEST_DB_ENV: _POSTGRES_TEST_DB,
        _POSTGRES_TEST_IMAGE_ENV: _POSTGRES_TEST_IMAGE,
        _POSTGRES_TEST_PASSWORD_ENV: _POSTGRES_TEST_PASSWORD,
        _POSTGRES_TEST_PORT_ENV: _POSTGRES_TEST_PORT,
        _POSTGRES_TEST_USER_ENV: _POSTGRES_TEST_USER,
    }
    assert _has_run(
        runs,
        "jq -r '.pg_search_version' src/gobby/data/postgres-pgsearch/version.json",
        "jq -r '.pg_search_sha256' src/gobby/data/postgres-pgsearch/version.json",
        _expected_database_authority_assignment(),
        _expected_database_host_assignment(),
        _expected_database_url_assignment(),
        "DATABASE_URL=$database_url",
        "GITHUB_ENV",
    )
    assert _has_run(
        runs,
        "docker build",
        '--build-arg PG_SEARCH_VERSION="${GOBBY_PG_SEARCH_VERSION}"',
        '--build-arg PG_SEARCH_SHA256="${GOBBY_PG_SEARCH_SHA256}"',
        '-t "${GOBBY_POSTGRES_TEST_IMAGE}"',
        "src/gobby/data/postgres-pgsearch",
    )
    assert _has_run(
        runs,
        "docker run --rm",
        '"${GOBBY_POSTGRES_TEST_IMAGE}"',
        "/usr/local/bin/pg_audit_export.sh",
        "--help",
    )
    assert _has_run(
        runs,
        "docker run -d",
        '--name "${GOBBY_POSTGRES_TEST_CONTAINER}"',
        '-e POSTGRES_DB="${GOBBY_POSTGRES_TEST_DB}"',
        '-e POSTGRES_USER="${GOBBY_POSTGRES_TEST_USER}"',
        '-e POSTGRES_PASSWORD="${GOBBY_POSTGRES_TEST_PASSWORD}"',
        '-p "${GOBBY_POSTGRES_TEST_PORT}:5432"',
        "--tmpfs /var/lib/postgresql",
        '"${GOBBY_POSTGRES_TEST_IMAGE}"',
        "postgres",
        *_PGAUDIT_COMMAND_OPTIONS,
    )
    assert _has_run(
        runs,
        "docker inspect",
        '"${GOBBY_POSTGRES_TEST_CONTAINER}"',
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


def _compose_default(env_name: str, default: str) -> str:
    return f"${{{env_name}:-{default}}}"


def _expected_database_url_assignment() -> str:
    return 'database_url="postgresql://${database_authority}/${GOBBY_POSTGRES_TEST_DB}"'


def _expected_database_authority_assignment() -> str:
    return 'database_authority="${GOBBY_POSTGRES_TEST_USER}:${GOBBY_POSTGRES_TEST_PASSWORD}"'


def _expected_database_host_assignment() -> str:
    return 'database_authority="${database_authority}@localhost:${GOBBY_POSTGRES_TEST_PORT}"'


def _job_env(env: Mapping[str, Any]) -> dict[str, str]:
    names = {
        _POSTGRES_TEST_CONTAINER_ENV,
        _POSTGRES_TEST_DB_ENV,
        _POSTGRES_TEST_IMAGE_ENV,
        _POSTGRES_TEST_PASSWORD_ENV,
        _POSTGRES_TEST_PORT_ENV,
        _POSTGRES_TEST_USER_ENV,
    }
    return {name: str(env[name]) for name in names}
