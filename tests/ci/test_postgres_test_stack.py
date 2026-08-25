"""Static contract tests for the PostgreSQL test stack and CI wiring."""

from __future__ import annotations

import json
import os
import subprocess
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
_POSTGRES_TEST_TMPFS_SIZE_GIB = 3
_POSTGRES_TEST_TMPFS_BYTES = _POSTGRES_TEST_TMPFS_SIZE_GIB * 1024**3
_POSTGRES_TEST_TMPFS_NR_INODES = 6_000_000
_POSTGRES_TEST_TMPFS = (
    f"/var/lib/postgresql:rw,size={_POSTGRES_TEST_TMPFS_SIZE_GIB}g"
    f",nr_inodes={_POSTGRES_TEST_TMPFS_NR_INODES}"
)
_POSTGRES_TEST_USER = "gobby_test"
_DEFAULT_POSTGRES_TEST_DSN = (
    f"postgresql://{_POSTGRES_TEST_USER}:{_POSTGRES_TEST_PASSWORD}"
    f"@localhost:{_POSTGRES_TEST_PORT}/{_POSTGRES_TEST_DB}"
)
_PGAUDIT_EMISSION_PROBE = "bash .github/scripts/verify-pgaudit-emission.sh"
_POSTGRES_SKIP_REASONS = [
    "DATABASE_URL must point at an isolated PostgreSQL test database",
    "PostgreSQL DSN required for hub runtime surface tests",
]
_PYTEST_FORBIDDEN_SKIP_REASONS = [
    "set GOBBY_RUN_AGY_PROBE=1",
    "set GOBBY_RUN_POSTGRES_TMPFS_FILL_TEST=1",
    "FalkorDB not reachable for integration benchmark",
    "set GOBBY_GROK_AUDIT_TRANSCRIPTS_DIR",
    "awaiting #14098",
    "set GOBBY_OTEL_COLLECTOR_SMOKE=1",
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

# Recorded during task #19485's 1,170-test, 627.57-second storage stress run.
_MEASURED_DATABASE_SIZE_PEAK_BYTES = 67_090_111
_MEASURED_TMPFS_USAGE_PEAK_BYTES = 191_967_232


def test_recorded_postgres_tmpfs_peak_has_headroom() -> None:
    assert _MEASURED_TMPFS_USAGE_PEAK_BYTES > _MEASURED_DATABASE_SIZE_PEAK_BYTES
    assert _POSTGRES_TEST_TMPFS_BYTES >= 10 * _MEASURED_TMPFS_USAGE_PEAK_BYTES


def test_test_compose_defines_ephemeral_postgres_test_service(repo_root: Path) -> None:
    compose_path = repo_root / "docker-compose.test.yml"

    assert compose_path.is_file(), "docker-compose.test.yml must define postgres-test"

    compose = _load_yaml(compose_path)
    assert compose["name"] == "gobby"
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
    assert postgres["tmpfs"] == [_POSTGRES_TEST_TMPFS]

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
        f"--tmpfs {_POSTGRES_TEST_TMPFS}",
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
    assert _has_run(runs, _PGAUDIT_EMISSION_PROBE)
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
        f"--tmpfs {_POSTGRES_TEST_TMPFS}",
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
    assert _has_run(runs, _PGAUDIT_EMISSION_PROBE)


def test_pgaudit_emission_probe_fails_without_update_audit_record(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    probe = repo_root / ".github/scripts/verify-pgaudit-emission.sh"
    fake_docker = tmp_path / "docker"
    fake_sleep = tmp_path / "sleep"
    call_log = tmp_path / "docker-calls.log"
    sql_log = tmp_path / "probe.sql"

    fake_docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${GOBBY_FAKE_DOCKER_LOG:?}"
if [[ "$*" == *" psql "* ]]; then
  cat > "${GOBBY_FAKE_SQL_LOG:?}"
  exit 0
fi
if [[ "$*" == *"grep -Eq"* ]]; then
  exit 1
fi
exit 0
"""
    )
    fake_sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_docker.chmod(0o755)
    fake_sleep.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "GOBBY_FAKE_DOCKER_LOG": str(call_log),
            "GOBBY_FAKE_SQL_LOG": str(sql_log),
            "GOBBY_POSTGRES_TEST_CONTAINER": _POSTGRES_TEST_CONTAINER,
            "GOBBY_POSTGRES_TEST_DB": _POSTGRES_TEST_DB,
            "GOBBY_POSTGRES_TEST_USER": _POSTGRES_TEST_USER,
            "PATH": f"{tmp_path}:{env['PATH']}",
        }
    )

    result = subprocess.run(
        ["bash", str(probe)],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode == 1
    assert "pgAudit emitted no AUDIT: SESSION record for the UPDATE probe" in result.stderr
    assert "UPDATE gobby_pgaudit_ci_probe" in sql_log.read_text()
    assert "LOG:  AUDIT: SESSION,.*UPDATE" in call_log.read_text()


def test_pre_push_resolves_and_exports_postgres_database_url_for_pytest(
    repo_root: Path,
) -> None:
    script = _load_pre_push_script(repo_root)

    assert "resolve_pytest_database_url()" in script
    assert 'if [ -n "${DATABASE_URL:-}" ]; then' in script
    assert "existing_postgres_test_dsn" in script
    assert "postgres_test_dsn_is_ready" in script
    assert "docker_compose -f docker-compose.test.yml up -d postgres-test" in script
    assert "${GOBBY_POSTGRES_TEST_PORT:-60892}" in script
    assert "PYTEST_DATABASE_URL=$(resolve_pytest_database_url)" in script
    assert 'DATABASE_URL="$PYTEST_DATABASE_URL"' in script
    assert 'GOBBY_POSTGRES_TEST_DSN="$PYTEST_DATABASE_URL"' in script
    resolve_fn = _bash_function(script, "resolve_pytest_database_url")
    _assert_before(
        resolve_fn,
        'if postgres_test_dsn_is_ready "$url"; then',
        "start_docker_postgres_test_database",
    )
    _assert_before(
        script,
        "PYTEST_DATABASE_URL=$(resolve_pytest_database_url)",
        'HOME="$PYTEST_ISOLATION_DIR/home"',
    )


def test_pre_push_reuses_reachable_existing_test_dsn(repo_root: Path, tmp_path: Path) -> None:
    completed = _run_resolve_harness(
        repo_root,
        tmp_path,
        body=(
            "postgres_test_dsn_is_ready() { return 0; }\n"
            "start_docker_postgres_test_database() { echo STARTED >&2; return 1; }\n"
            "unset DATABASE_URL\n"
            "resolve_pytest_database_url\n"
        ),
        env=_env_without_database_url(),
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == _DEFAULT_POSTGRES_TEST_DSN
    assert "STARTED" not in completed.stderr


def test_pre_push_starts_compose_when_existing_test_dsn_is_down(
    repo_root: Path, tmp_path: Path
) -> None:
    completed = _run_resolve_harness(
        repo_root,
        tmp_path,
        body=(
            "postgres_test_dsn_is_ready() { return 1; }\n"
            "start_docker_postgres_test_database() {\n"
            "    echo COMPOSE >&2\n"
            "    existing_postgres_test_dsn\n"
            "}\n"
            "unset DATABASE_URL\n"
            "resolve_pytest_database_url\n"
        ),
        env=_env_without_database_url(),
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == _DEFAULT_POSTGRES_TEST_DSN
    assert "COMPOSE" in completed.stderr


def test_pre_push_prefers_exported_database_url_over_existing_test_dsn(
    repo_root: Path, tmp_path: Path
) -> None:
    exported = "postgresql://custom:pw@127.0.0.1:1/custom"
    env = _env_without_database_url()
    env["DATABASE_URL"] = exported
    completed = _run_resolve_harness(
        repo_root,
        tmp_path,
        body=(
            "postgres_test_dsn_is_ready() { echo READY >&2; return 0; }\n"
            "start_docker_postgres_test_database() { echo STARTED >&2; return 1; }\n"
            "resolve_pytest_database_url\n"
        ),
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == exported
    assert "READY" not in completed.stderr
    assert "STARTED" not in completed.stderr


def test_pre_push_never_resolves_the_live_hub_dsn_for_pytest(repo_root: Path) -> None:
    """The suite drops schemas, so pre-push must never hand it the daemon's hub."""
    script = _load_pre_push_script(repo_root)

    assert "read_bootstrap_database_url" not in script
    assert "load_bootstrap(resolve_database_url=True).database_url" not in script


def test_pre_push_records_manifest_and_checks_source_integrity(repo_root: Path) -> None:
    script = _load_pre_push_script(repo_root)

    assert 'MANIFEST_PATH="$REPORTS_DIR/pre-push-$TIMESTAMP.json"' in script
    assert 'python3 "$MANIFEST_TOOL" start --manifest "$MANIFEST_PATH" --repo-root .' in script
    assert 'python3 "$MANIFEST_TOOL" record' in script
    assert 'python3 "$MANIFEST_TOOL" finish' in script
    assert 'record_command_result "pytest" "$PYTEST_EXIT" "$PYTEST_REPORT"' in script
    assert "report manifest invalidated" in script


def test_frontend_format_check_is_enforced_in_ci_and_pre_push(repo_root: Path) -> None:
    workflow = _load_yaml(repo_root / ".github/workflows/ci.yml")
    lint_frontend = _mapping(_mapping(workflow["jobs"])["lint-frontend"])
    ci_runs = _step_runs(_sequence(lint_frontend["steps"]))

    assert "npm run format:check" in ci_runs
    assert ci_runs.index("npm run format:check") < ci_runs.index("npm run lint")

    short_script = _load_pre_push_short_script(repo_root)
    assert (
        '(cd web && npm run format:check) 2>&1 | tee "$REPORTS_DIR/frontend-format-$TIMESTAMP.txt"'
    ) in short_script
    assert "frontend_format_status=${PIPESTATUS[0]}" in short_script
    _assert_before(short_script, "npm run format:check", "npx tsc --noEmit")

    full_script = _load_pre_push_script(repo_root)
    assert 'FRONTEND_FORMAT_REPORT="$REPORTS_DIR/frontend-format-$TIMESTAMP.txt"' in full_script
    assert (
        'record_command_result "frontend-format" "$FRONTEND_FORMAT_EXIT" "$FRONTEND_FORMAT_REPORT"'
    ) in full_script
    _assert_before(full_script, "npm run format:check", "npx tsc --noEmit")


def test_python_suppression_ratchet_runs_in_ci_and_pre_push(repo_root: Path) -> None:
    command = "gobby test-types suppressions . --baseline .gobby/python-suppressions-baseline.json"
    workflow = _load_yaml(repo_root / ".github/workflows/ci.yml")
    typecheck = _mapping(_mapping(workflow["jobs"])["typecheck"])
    ci_runs = _step_runs(_sequence(typecheck["steps"]))

    assert any(command in run for run in ci_runs)
    full_script = _load_pre_push_script(repo_root).replace("\\\n", "")
    short_script = _load_pre_push_short_script(repo_root).replace("\\\n", "")
    assert command in " ".join(full_script.split())
    assert command in " ".join(short_script.split())


def test_pre_push_exports_managed_falkordb_settings_before_home_isolation(
    repo_root: Path,
) -> None:
    script = _load_pre_push_script(repo_root)
    dream_e2e = (repo_root / "tests/e2e/test_memory_dream_gc_e2e.py").read_text()

    assert "read_managed_falkordb_settings()" in script
    assert "print(runtime.environment[name])" in script
    for name in (
        "GOBBY_FALKORDB_HOST",
        "GOBBY_FALKORDB_PORT",
        "GOBBY_FALKORDB_PASSWORD",
    ):
        assert f'"{name}"' in script
        assert f'{name}="$PYTEST_FALKORDB_{name.removeprefix("GOBBY_FALKORDB_")}"' in script
        assert f'"{name}"' in dream_e2e
    assert 'GOBBY_TEST_FALKOR_HOST="$PYTEST_FALKORDB_HOST"' in script
    assert 'GOBBY_TEST_FALKOR_PORT="$PYTEST_FALKORDB_PORT"' in script
    assert 'GOBBY_TEST_FALKOR_PASSWORD="$PYTEST_FALKORDB_PASSWORD"' in script
    assert "os.environ.get(name)" in dream_e2e
    _assert_before(
        script,
        "elif ! load_pytest_falkordb_settings; then",
        'HOME="$PYTEST_ISOLATION_DIR/home"',
    )


def test_pre_push_uses_canonical_logging_dir_for_pytest(repo_root: Path) -> None:
    script = _load_pre_push_script(repo_root)

    assert 'GOBBY_LOGGING_DIR="$PYTEST_ISOLATION_DIR/logs"' in script
    for legacy_name in (
        "GOBBY_LOGGING_CLIENT",
        "GOBBY_LOGGING_CLIENT_ERROR",
        "GOBBY_LOGGING_CLIENT_STDERR",
        "GOBBY_LOGGING_MCP_SERVER",
        "GOBBY_LOGGING_MCP_CLIENT",
        "GOBBY_LOGGING_HOOK_MANAGER",
    ):
        assert legacy_name not in script


def test_pre_push_fails_if_postgres_skip_reason_reaches_pytest_report(
    repo_root: Path,
) -> None:
    script = _load_pre_push_script(repo_root)

    assert "POSTGRES_SKIP_REASONS=(" in script
    assert "PYTEST_FORBIDDEN_SKIP_REASONS=(" in script
    for reason in _POSTGRES_SKIP_REASONS:
        assert reason in script
    for reason in _PYTEST_FORBIDDEN_SKIP_REASONS:
        assert reason in script
    assert "check_pytest_postgres_skip_guard()" in script
    assert (
        'for reason in "${POSTGRES_SKIP_REASONS[@]}" "${PYTEST_FORBIDDEN_SKIP_REASONS[@]}"; do'
    ) in script
    assert 'grep -q "$reason" "$report_path"' in script
    assert 'uv_run pytest "${PYTEST_SELECTION_ARGS[@]}" -v --tb=line -rFEsw' in script
    assert 'check_pytest_postgres_skip_guard "$PYTEST_REPORT"' in script
    assert "GOBBY_RUN_AGY_PROBE=1" in script
    assert "GOBBY_RUN_POSTGRES_TMPFS_FILL_TEST=1" in script
    assert "GOBBY_OTEL_COLLECTOR_SMOKE=1" in script
    assert (
        'GOBBY_GROK_AUDIT_TRANSCRIPTS_DIR="$PWD/tests/sessions/transcripts/fixtures/grok_audit"'
    ) in script
    assert "command -v agy" in script
    assert "agy not found on PATH" in script
    _assert_before(script, "command -v agy", "uv_run pytest")

    fill_probe = (repo_root / "tests/ci/test_postgres_tmpfs_fill.py").read_text()
    assert "/usr/lib/postgresql/18/bin" not in fill_probe
    assert "refusing to fill /dev/shm" not in fill_probe
    assert "mount -t tmpfs" in fill_probe


def test_pre_push_supports_local_all_extras_uv_run_opt_in(repo_root: Path) -> None:
    script = _load_pre_push_script(repo_root)

    _assert_uv_run_all_extras_helper(script)
    assert "uv_run ruff check src/ --fix --no-unsafe-fixes" in script
    mypy_command = next(line for line in script.splitlines() if "uv_run mypy src/" in line)
    assert "--strict" in mypy_command
    assert "--no-incremental" in mypy_command
    assert "uv_run bandit -c pyproject.toml -r src/ -q" in script
    assert "uv_run pip-audit" in script
    assert "uv_run gobby test-quality audit" in script


def test_pre_push_runs_rust_workspace_tests(repo_root: Path) -> None:
    script = _load_pre_push_script(repo_root)

    assert 'CARGO_REPORT="$REPORTS_DIR/cargo-$TIMESTAMP.txt"' in script
    assert "cargo nextest run --profile ci --workspace --no-default-features" in script
    assert "cargo test --doc --workspace --no-default-features" in script
    assert '} 2>&1 | tee "$CARGO_REPORT"; then' in script

    cargo_section = script[script.index("# Cargo -") : script.index("# Bandit -")]
    assert "FAILED=1" in cargo_section
    _assert_before(
        cargo_section,
        "cargo nextest run --profile ci --workspace --no-default-features",
        "cargo test --doc --workspace --no-default-features",
    )


def test_pre_push_short_supports_local_all_extras_uv_run_opt_in(repo_root: Path) -> None:
    script = _load_pre_push_short_script(repo_root)

    _assert_uv_run_all_extras_helper(script)
    assert "uv_run ruff check src/ --fix --no-unsafe-fixes" in script
    assert "uv_run ruff format src/" in script
    assert "uv_run mypy src/ --strict" in script
    assert "uv_run bandit -c pyproject.toml -r src/ -q" in script
    assert "uv_run pip-audit" in script


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


def _bash_function(script: str, name: str) -> str:
    header = f"{name}() {{"
    start = script.index(header)
    end = script.index("\n}\n", start)
    return script[start : end + 3]


def _env_without_database_url() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}


def _run_resolve_harness(
    repo_root: Path,
    tmp_path: Path,
    *,
    body: str,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    script = _load_pre_push_script(repo_root)
    harness = tmp_path / "resolve-dsn.sh"
    harness.write_text(
        "\n".join(
            (
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                _bash_function(script, "existing_postgres_test_dsn"),
                _bash_function(script, "resolve_pytest_database_url"),
                body,
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return subprocess.run(
        ("bash", str(harness)),
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _load_pre_push_short_script(repo_root: Path) -> str:
    return (repo_root / "pre-push-test-short.sh").read_text()


def _assert_uv_run_all_extras_helper(script: str) -> None:
    assert "UV_EXTRA_FLAGS=()" in script
    assert 'if [ "${GOBBY_UV_ALL_EXTRAS:-}" = "1" ]; then' in script
    assert "UV_EXTRA_FLAGS=(--all-extras)" in script
    assert "uv_run()" in script
    assert 'if [ "${#UV_EXTRA_FLAGS[@]}" -gt 0 ]; then' in script
    assert 'uv run "${UV_EXTRA_FLAGS[@]}" "$@"' in script
    assert 'uv run "$@"' in script


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
