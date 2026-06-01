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
_POSTGRES_SKIP_REASONS = [
    "DATABASE_URL or configured bootstrap database_url is required",
    "PostgreSQL DSN required for hub runtime surface tests",
]

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


def test_ci_build_job_runs_wheel_smoke_against_local_postgres(repo_root: Path) -> None:
    workflow = _load_yaml(repo_root / ".github/workflows/ci.yml")
    build_job = _mapping(_mapping(workflow["jobs"])["build"])
    runs = _step_runs(_sequence(build_job["steps"]))
    env = _mapping(build_job.get("env", {}))

    assert "services" not in build_job, "CI must local-build Postgres instead of pulling a service"
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
        _expected_database_authority_assignment(),
        _expected_database_host_assignment(),
        _expected_database_url_assignment(),
        'echo "DATABASE_URL=$database_url"',
    )
    assert _has_run(
        runs,
        "docker build",
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
        "find dist -maxdepth 1 -name 'gobby-*.whl' -print -quit",
        "GOBBY_RUN_WHEEL_UI_SMOKE=1",
        'GOBBY_WHEEL_PATH="$wheel"',
        "uv run pytest tests/packaging/test_installed_wheel_ui_smoke.py -v",
    )


def test_pre_push_resolves_and_exports_postgres_database_url_for_pytest(
    repo_root: Path,
) -> None:
    script = _load_pre_push_script(repo_root)

    assert "resolve_pytest_database_url()" in script
    assert 'if [ -n "${DATABASE_URL:-}" ]; then' in script
    assert "read_bootstrap_database_url" in script
    assert "load_bootstrap(resolve_database_url=True).database_url" in script
    assert "docker_compose -f docker-compose.test.yml up -d postgres-test" in script
    assert "${GOBBY_POSTGRES_TEST_PORT:-60892}" in script
    assert "PYTEST_DATABASE_URL=$(resolve_pytest_database_url)" in script
    assert 'DATABASE_URL="$PYTEST_DATABASE_URL"' in script
    assert 'GOBBY_POSTGRES_TEST_DSN="$PYTEST_DATABASE_URL"' in script
    _assert_before(
        script,
        "PYTEST_DATABASE_URL=$(resolve_pytest_database_url)",
        'HOME="$PYTEST_ISOLATION_DIR/home"',
    )


def test_pre_push_fails_if_postgres_skip_reason_reaches_pytest_report(
    repo_root: Path,
) -> None:
    script = _load_pre_push_script(repo_root)

    assert "POSTGRES_SKIP_REASONS=(" in script
    for reason in _POSTGRES_SKIP_REASONS:
        assert reason in script
    assert "check_pytest_postgres_skip_guard()" in script
    assert 'for reason in "${POSTGRES_SKIP_REASONS[@]}"; do' in script
    assert 'grep -q "$reason" "$report_path"' in script
    assert 'uv_run pytest "${PYTEST_SELECTION_ARGS[@]}" -v --tb=line -rFEsw' in script
    assert 'check_pytest_postgres_skip_guard "$PYTEST_REPORT"' in script


def test_pre_push_supports_local_all_extras_uv_run_opt_in(repo_root: Path) -> None:
    script = _load_pre_push_script(repo_root)

    assert "UV_EXTRA_FLAGS=()" in script
    assert 'if [ "${GOBBY_UV_ALL_EXTRAS:-}" = "1" ]; then' in script
    assert "UV_EXTRA_FLAGS=(--all-extras)" in script
    assert "uv_run()" in script
    assert 'uv run "${UV_EXTRA_FLAGS[@]}" "$@"' in script
    assert "uv_run ruff check src/ --fix --no-unsafe-fixes" in script
    assert "uv_run mypy src/ --strict --no-incremental" in script
    assert "uv_run bandit -c pyproject.toml -r src/ -q" in script
    assert "uv_run pip-audit" in script
    assert "uv_run gobby test-quality audit" in script


def test_pre_push_excludes_live_opt_in_tests_by_default(repo_root: Path) -> None:
    script = _load_pre_push_script(repo_root)

    assert "PYTEST_SELECTION_ARGS=()" in script
    assert "GOBBY_RUN_PRE_PUSH_SANDBOX" in script
    assert "PYTEST_SELECTION_ARGS+=(--ignore=tests/integration/sandbox)" in script
    assert "GOBBY_RUN_WHEEL_UI_SMOKE" in script
    assert "tests/packaging/test_installed_wheel_ui_smoke.py" in script
    assert "GOBBY_RUN_DROID_HOOK_INTEGRATION" in script
    assert (
        "tests/agents/test_spawn_executor_droid.py::"
        "test_droid_worktree_spawn_fires_pre_tool_use_against_gobby_daemon"
    ) in script
    assert "GOBBY_RUN_BUILD_CANARY" in script
    assert (
        "tests/e2e/test_build_dispatcher_autonomy.py::test_real_small_gobby_build_canary" in script
    )
    assert "GOBBY_RUN_E2E_SESSION_LIFECYCLE" in script
    assert "tests/sessions/test_e2e_session_tracking.py::test_full_lifecycle" in script


def _load_yaml(path: Path) -> Mapping[str, Any]:
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


def _load_pre_push_script(repo_root: Path) -> str:
    return (repo_root / "pre-push-test.sh").read_text()


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


def _assert_before(content: str, before: str, after: str) -> None:
    assert content.index(before) < content.index(after)


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
