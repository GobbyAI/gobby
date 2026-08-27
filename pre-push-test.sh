#!/usr/bin/env bash
set -euo pipefail

# Pre-push CI/CD test suite
# Runs linting, type checking, security scanning, and tests

# Check for moreutils (provides `ts` for timestamps)
if command -v ts &> /dev/null; then
    timestamp() { ts '[%H:%M:%S]'; }
else
    echo "Note: Install moreutils for per-line timestamps (brew install moreutils)"
    timestamp() { cat; }
fi

TIMESTAMP=$(date +%s)
REPORTS_DIR="./reports"
MANIFEST_TOOL="./pre-push-manifest.py"
MANIFEST_PATH="$REPORTS_DIR/pre-push-$TIMESTAMP.json"
mkdir -p "$REPORTS_DIR"
rm -f "$REPORTS_DIR"/*.txt "$REPORTS_DIR"/*.md "$REPORTS_DIR"/*.log "$REPORTS_DIR"/*.json 2>/dev/null || true

echo "=== Pre-push Test Suite ==="
echo "Timestamp: $TIMESTAMP"
echo ""

# Track failures
FAILED=0
MANIFEST_FINALIZED=0
PYTEST_ISOLATION_DIR=""
PYTEST_FALKORDB_HOST=""
PYTEST_FALKORDB_PORT=""
PYTEST_FALKORDB_PASSWORD=""
UV_EXTRA_FLAGS=()
if [ "${GOBBY_UV_ALL_EXTRAS:-}" = "1" ]; then
    UV_EXTRA_FLAGS=(--all-extras)
fi
PIP_AUDIT_IGNORE_ARGS=(
    # Keep local reports aligned with .github/workflows/ci.yml no-fix advisories.
    --ignore-vuln CVE-2025-69872
    --ignore-vuln CVE-2026-4539
    --ignore-vuln CVE-2025-3000
)
POSTGRES_SKIP_REASONS=(
    "DATABASE_URL must point at an isolated PostgreSQL test database"
    "PostgreSQL DSN required for hub runtime surface tests"
)
PYTEST_FORBIDDEN_SKIP_REASONS=(
    "set GOBBY_RUN_AGY_PROBE=1"
    "set GOBBY_RUN_POSTGRES_TMPFS_FILL_TEST=1"
    "FalkorDB not reachable for integration benchmark"
    "set GOBBY_GROK_AUDIT_TRANSCRIPTS_DIR"
    "awaiting #14098"
    "set GOBBY_OTEL_COLLECTOR_SMOKE=1"
)
PYTEST_SELECTION_ARGS=()
if [ "${GOBBY_RUN_PRE_PUSH_SANDBOX:-}" = "1" ]; then
    PYTEST_SELECTION_ARGS+=(--run-sandbox)
else
    PYTEST_SELECTION_ARGS+=(--ignore=tests/integration/sandbox)
fi
if [ "${GOBBY_RUN_WHEEL_UI_SMOKE:-}" != "1" ]; then
    PYTEST_SELECTION_ARGS+=(--ignore=tests/packaging/test_installed_wheel_ui_smoke.py)
fi
if [ "${GOBBY_RUN_DROID_HOOK_INTEGRATION:-}" != "1" ]; then
    PYTEST_SELECTION_ARGS+=(
        --deselect=tests/agents/test_spawn_executor_droid.py::test_droid_worktree_spawn_fires_pre_tool_use_against_gobby_daemon
    )
fi
if [ "${GOBBY_RUN_BUILD_CANARY:-}" != "1" ]; then
    PYTEST_SELECTION_ARGS+=(
        --deselect=tests/e2e/test_build_dispatcher_autonomy.py::test_real_small_gobby_build_canary
    )
fi
if [ "${GOBBY_RUN_E2E_SESSION_LIFECYCLE:-}" != "1" ]; then
    PYTEST_SELECTION_ARGS+=(
        --deselect=tests/sessions/test_e2e_session_tracking.py::test_full_lifecycle
    )
fi

uv_run() {
    if [ "${#UV_EXTRA_FLAGS[@]}" -gt 0 ]; then
        uv run "${UV_EXTRA_FLAGS[@]}" "$@"
    else
        uv run "$@"
    fi
}

check_committed_bundled_manifest() {
    uv_run python -m gobby.install.manifest --repo-root . --treeish HEAD
}

if [ "${1:-}" = "--bundled-manifest-only" ]; then
    if check_committed_bundled_manifest; then
        exit 0
    else
        exit $?
    fi
fi

# shellcheck disable=SC2329  # Invoked indirectly by the EXIT trap below.
cleanup() {
    local exit_code=$?
    trap - EXIT
    if [ "$MANIFEST_FINALIZED" -eq 0 ] && [ -f "$MANIFEST_PATH" ]; then
        python3 "$MANIFEST_TOOL" finish \
            --manifest "$MANIFEST_PATH" \
            --status failed >/dev/null 2>&1 || true
    fi
    if [ -n "${PYTEST_ISOLATION_DIR:-}" ] && [ -d "$PYTEST_ISOLATION_DIR" ]; then
        rm -rf "$PYTEST_ISOLATION_DIR"
    fi
    exit "$exit_code"
}
trap cleanup EXIT

docker_compose() {
    if docker compose version >/dev/null 2>&1; then
        docker compose "$@"
    elif command -v docker-compose >/dev/null 2>&1; then
        docker-compose "$@"
    else
        return 127
    fi
}

read_managed_falkordb_settings() {
    uv_run python - <<'PY'
from pathlib import Path

from gobby.cli.installers.compose_env import resolve_compose_runtime

runtime = resolve_compose_runtime(
    Path.home() / ".gobby",
    profiles=("falkordb",),
)
for name in (
    "GOBBY_FALKORDB_HOST",
    "GOBBY_FALKORDB_PORT",
    "GOBBY_FALKORDB_PASSWORD",
):
    print(runtime.environment[name])
PY
}

load_pytest_falkordb_settings() {
    {
        IFS= read -r PYTEST_FALKORDB_HOST &&
            IFS= read -r PYTEST_FALKORDB_PORT &&
            IFS= read -r PYTEST_FALKORDB_PASSWORD
    } < <(read_managed_falkordb_settings)
    [ -n "$PYTEST_FALKORDB_HOST" ] &&
        [ -n "$PYTEST_FALKORDB_PORT" ] &&
        [ -n "$PYTEST_FALKORDB_PASSWORD" ]
}

# bash 3.2 + set -u: empty "${arr[@]}" is unbound; "$@" is not.
record_command_result() {
    local name="$1"
    local exit_code="$2"
    local report_path="$3"
    local non_gating="${4:-}"
    if [ "$non_gating" = "non-gating" ]; then
        set -- --non-gating
    else
        set --
    fi
    local manifest_exit=0
    if python3 "$MANIFEST_TOOL" record \
        --manifest "$MANIFEST_PATH" \
        --name "$name" \
        --report "$report_path" \
        --exit-code "$exit_code" \
        "$@"; then
        return 0
    else
        manifest_exit=$?
    fi
    if [ "$manifest_exit" -eq 2 ]; then
        MANIFEST_FINALIZED=1
        echo "✗ Source changed during the pre-push run; report manifest invalidated"
    else
        echo "✗ Failed to update the pre-push report manifest"
    fi
    exit 1
}

record_skipped_command() {
    local name="$1"
    local non_gating="${2:-}"
    if [ "$non_gating" = "non-gating" ]; then
        set -- --non-gating
    else
        set --
    fi
    local manifest_exit=0
    if python3 "$MANIFEST_TOOL" record \
        --manifest "$MANIFEST_PATH" \
        --name "$name" \
        --status skipped \
        "$@"; then
        return 0
    else
        manifest_exit=$?
    fi
    if [ "$manifest_exit" -eq 2 ]; then
        MANIFEST_FINALIZED=1
        echo "✗ Source changed during the pre-push run; report manifest invalidated"
    else
        echo "✗ Failed to update the pre-push report manifest"
    fi
    exit 1
}

start_docker_postgres_test_database() {
    if [ ! -f docker-compose.test.yml ]; then
        echo "docker-compose.test.yml is required for the fallback PostgreSQL test database." >&2
        return 1
    fi

    if ! docker_compose version >/dev/null 2>&1; then
        echo "Docker Compose is required when DATABASE_URL and bootstrap database_url are unset." >&2
        return 1
    fi

    echo "Starting postgres-test from docker-compose.test.yml..." >&2
    if ! docker_compose -f docker-compose.test.yml up -d postgres-test >&2; then
        echo "Failed to start postgres-test from docker-compose.test.yml." >&2
        return 1
    fi

    local container_id
    container_id=$(
        docker_compose -f docker-compose.test.yml ps --status running -q postgres-test 2>/dev/null || true
    )
    if [ -z "$container_id" ]; then
        echo "Could not find the postgres-test container after startup." >&2
        return 1
    fi

    local attempt
    local health_status
    attempt=1
    while [ "$attempt" -le 30 ]; do
        health_status=$(docker inspect \
            --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
            "$container_id" 2>/dev/null || true)
        if [ "$health_status" = "healthy" ] || [ "$health_status" = "running" ]; then
            existing_postgres_test_dsn
            return 0
        fi
        if [ "$health_status" = "unhealthy" ] \
            || [ "$health_status" = "exited" ] \
            || [ "$health_status" = "dead" ]; then
            echo "postgres-test container is $health_status." >&2
            docker logs "$container_id" >&2 || true
            return 1
        fi
        sleep 2
        attempt=$((attempt + 1))
    done

    echo "Timed out waiting for postgres-test to become healthy." >&2
    docker logs "$container_id" >&2 || true
    return 1
}

# Isolated test stack already used by docker-compose.test.yml and any other
# container publishing 60892 (for example gobby-postgres-test-1). Never the
# live hub on 60891.
existing_postgres_test_dsn() {
    printf 'postgresql://%s:%s@localhost:%s/%s\n' \
        "${GOBBY_POSTGRES_TEST_USER:-gobby_test}" \
        "${GOBBY_POSTGRES_TEST_PASSWORD:-gobby_test}" \
        "${GOBBY_POSTGRES_TEST_PORT:-60892}" \
        "${GOBBY_POSTGRES_TEST_DB:-gobby_test}"
}

postgres_test_dsn_is_ready() {
    local url="$1"
    uv_run python - "$url" <<'PY' >/dev/null 2>&1
import sys

import psycopg

try:
    with psycopg.connect(sys.argv[1], connect_timeout=2) as conn:
        conn.execute("SELECT 1")
except Exception:
    raise SystemExit(1)
PY
}

# Never resolve the bootstrap database_url here: that is the hub the running
# daemon owns, and the suite drops schemas and terminates backends on whatever
# database it is handed.
resolve_pytest_database_url() {
    if [ -n "${DATABASE_URL:-}" ]; then
        printf '%s\n' "$DATABASE_URL"
        return 0
    fi

    local url
    url=$(existing_postgres_test_dsn)
    if postgres_test_dsn_is_ready "$url"; then
        printf '%s\n' "$url"
        return 0
    fi

    start_docker_postgres_test_database
}

check_pytest_postgres_skip_guard() {
    local report_path="$1"
    local reason
    for reason in "${POSTGRES_SKIP_REASONS[@]}" "${PYTEST_FORBIDDEN_SKIP_REASONS[@]}"; do
        if [ -f "$report_path" ] && grep -q "$reason" "$report_path"; then
            echo "✗ Pytest skipped required coverage"
            echo "  Report contains: $reason"
            return 1
        fi
    done
    return 0
}

python3 "$MANIFEST_TOOL" start --manifest "$MANIFEST_PATH" --repo-root .

# Bundled templates and their packaged manifest must describe the same committed
# tree. The checker reads Git blobs, so unrelated worktree dirt cannot skew it.
echo ">>> Checking committed bundled-content manifest..."
BUNDLED_MANIFEST_REPORT="$REPORTS_DIR/bundled-manifest-$TIMESTAMP.txt"
BUNDLED_MANIFEST_EXIT=0
if check_committed_bundled_manifest 2>&1 | tee "$BUNDLED_MANIFEST_REPORT"; then
    echo "✓ Committed bundled-content manifest passed"
else
    BUNDLED_MANIFEST_EXIT=$?
    echo "✗ Committed bundled-content manifest failed"
    FAILED=1
fi
record_command_result "bundled-manifest" "$BUNDLED_MANIFEST_EXIT" "$BUNDLED_MANIFEST_REPORT"
echo ""

# Ruff - autofix safe changes only (no unsafe fixes)
echo ">>> Running ruff check + format..."
RUFF_REPORT="$REPORTS_DIR/ruff-$TIMESTAMP.txt"
RUFF_EXIT=0
if {
    uv_run ruff check src/ --fix --no-unsafe-fixes &&
        uv_run ruff format src/
} 2>&1 | tee "$RUFF_REPORT"; then
    echo "✓ Ruff passed"
else
    RUFF_EXIT=$?
    echo "✗ Ruff failed"
    FAILED=1
fi
record_command_result "ruff" "$RUFF_EXIT" "$RUFF_REPORT"
echo ""

# Mypy - strict mode
echo ">>> Running mypy (strict)..."
MYPY_REPORT="$REPORTS_DIR/mypy-$TIMESTAMP.txt"
MYPY_EXIT=0
if uv_run mypy src/ --no-incremental --strict 2>&1 | tee "$MYPY_REPORT"; then
    echo "✓ Mypy passed"
else
    MYPY_EXIT=$?
    echo "✗ Mypy failed"
    FAILED=1
fi
record_command_result "mypy" "$MYPY_EXIT" "$MYPY_REPORT"
echo ""

# Python suppression ratchet
echo ">>> Checking Python suppression debt..."
SUPPRESSIONS_REPORT="$REPORTS_DIR/python-suppressions-$TIMESTAMP.txt"
SUPPRESSIONS_EXIT=0
if uv_run gobby test-types suppressions . \
    --baseline .gobby/python-suppressions-baseline.json \
    2>&1 | tee "$SUPPRESSIONS_REPORT"; then
    echo "✓ Python suppression ratchet passed"
else
    SUPPRESSIONS_EXIT=$?
    echo "✗ Python suppression ratchet failed"
    FAILED=1
fi
record_command_result "python-suppressions" "$SUPPRESSIONS_EXIT" "$SUPPRESSIONS_REPORT"
echo ""

# Prettier - frontend formatting
echo ">>> Checking frontend formatting..."
FRONTEND_FORMAT_REPORT="$REPORTS_DIR/frontend-format-$TIMESTAMP.txt"
FRONTEND_FORMAT_EXIT=0
if (cd web && npm run format:check) 2>&1 | tee "$FRONTEND_FORMAT_REPORT"; then
    echo "✓ Frontend format check passed"
else
    FRONTEND_FORMAT_EXIT=$?
    echo "✗ Frontend format check failed"
    FAILED=1
fi
record_command_result "frontend-format" "$FRONTEND_FORMAT_EXIT" "$FRONTEND_FORMAT_REPORT"
echo ""

# TypeScript - frontend type checking
echo ">>> Running TypeScript check..."
TSC_REPORT="$REPORTS_DIR/tsc-$TIMESTAMP.txt"
TSC_EXIT=0
if (cd web && npx tsc --noEmit) 2>&1 | tee "$TSC_REPORT"; then
    echo "✓ TypeScript passed"
else
    TSC_EXIT=$?
    echo "✗ TypeScript failed"
    FAILED=1
fi
record_command_result "typescript" "$TSC_EXIT" "$TSC_REPORT"
echo ""

# ESLint - frontend linting
echo ">>> Running frontend lint..."
ESLINT_REPORT="$REPORTS_DIR/eslint-$TIMESTAMP.txt"
ESLINT_EXIT=0
if (cd web && npm run lint) 2>&1 | tee "$ESLINT_REPORT"; then
    echo "✓ Frontend lint passed"
else
    ESLINT_EXIT=$?
    echo "✗ Frontend lint failed"
    FAILED=1
fi
record_command_result "frontend-lint" "$ESLINT_EXIT" "$ESLINT_REPORT"
echo ""

# Vitest - frontend tests with coverage
echo ">>> Running vitest..."
VITEST_REPORT="$REPORTS_DIR/vitest-$TIMESTAMP.txt"
VITEST_EXIT=0
if (cd web && npx vitest run --coverage) 2>&1 | tee "$VITEST_REPORT"; then
    echo "✓ Vitest passed"
else
    VITEST_EXIT=$?
    echo "✗ Vitest failed"
    FAILED=1
fi
record_command_result "vitest" "$VITEST_EXIT" "$VITEST_REPORT"
echo ""

# Cargo - Rust workspace tests matching the canonical CI feature set
echo ">>> Running Cargo workspace tests..."
CARGO_REPORT="$REPORTS_DIR/cargo-$TIMESTAMP.txt"
CARGO_EXIT=0
if {
    cargo nextest run --profile ci --workspace --no-default-features &&
        cargo test --doc --workspace --no-default-features
} 2>&1 | tee "$CARGO_REPORT"; then
    echo "✓ Cargo tests passed"
else
    CARGO_EXIT=$?
    echo "✗ Cargo tests failed"
    FAILED=1
fi
record_command_result "cargo" "$CARGO_EXIT" "$CARGO_REPORT"
echo ""

# Bandit - security linting
echo ">>> Running bandit..."
BANDIT_REPORT="$REPORTS_DIR/bandit-$TIMESTAMP.txt"
BANDIT_EXIT=0
if uv_run bandit -c pyproject.toml -r src/ -q 2>&1 | tee "$BANDIT_REPORT"; then
    echo "✓ Bandit passed"
else
    BANDIT_EXIT=$?
    echo "✗ Bandit failed"
    FAILED=1
fi
record_command_result "bandit" "$BANDIT_EXIT" "$BANDIT_REPORT"
echo ""

# pip-audit - dependency CVE scanning
echo ">>> Running pip-audit..."
PIP_AUDIT_REPORT="$REPORTS_DIR/pip-audit-$TIMESTAMP.txt"
PIP_AUDIT_EXIT=0
if uv_run pip-audit "${PIP_AUDIT_IGNORE_ARGS[@]}" 2>&1 | tee "$PIP_AUDIT_REPORT"; then
    echo "✓ pip-audit passed"
else
    PIP_AUDIT_EXIT=$?
    echo "✗ pip-audit failed"
    FAILED=1
fi
record_command_result "pip-audit" "$PIP_AUDIT_EXIT" "$PIP_AUDIT_REPORT"
echo ""

# Test quality - static audit against tracked baseline
echo ">>> Running test-quality audit..."
TEST_QUALITY_REPORT="$REPORTS_DIR/test-quality-$TIMESTAMP.txt"
TEST_QUALITY_EXIT=0
if uv_run gobby test-quality audit --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high 2>&1 | tee "$TEST_QUALITY_REPORT"; then
    echo "✓ Test-quality audit passed"
else
    TEST_QUALITY_EXIT=$?
    echo "✗ Test-quality audit failed"
    FAILED=1
fi
record_command_result "test-quality" "$TEST_QUALITY_EXIT" "$TEST_QUALITY_REPORT"
echo ""

# Pytest - tests with coverage report. Keep HOME/GOBBY_HOME isolated while
# passing an explicit PostgreSQL test target so DB coverage cannot silently skip.
# Uses verbose mode with timestamps to correlate test execution with daemon logs
echo ">>> Running pytest with coverage..."
PYTEST_ISOLATION_DIR=$(mktemp -d "${TMPDIR:-/tmp}/gobby-pre-push-${TIMESTAMP}.XXXXXX")
PYTEST_REPORT="$REPORTS_DIR/pytest-$TIMESTAMP.txt"
PYTEST_EXIT=0
if ! PYTEST_DATABASE_URL=$(resolve_pytest_database_url); then
    PYTEST_EXIT=1
    echo "✗ Failed to resolve PostgreSQL DATABASE_URL for pytest"
    echo "  Set DATABASE_URL to an isolated test database, or enable Docker so"
    echo "  docker-compose.test.yml can start postgres-test on port 60892."
    FAILED=1
elif ! mkdir -p \
    "$PYTEST_ISOLATION_DIR/home" \
    "$PYTEST_ISOLATION_DIR/gobby-home" \
    "$PYTEST_ISOLATION_DIR/logs" \
    "$PYTEST_ISOLATION_DIR/hooks"; then
    PYTEST_EXIT=1
    echo "✗ Failed to create pytest isolation directories under PYTEST_ISOLATION_DIR=$PYTEST_ISOLATION_DIR"
    echo "  Check directory permissions and available disk space."
    FAILED=1
elif ! load_pytest_falkordb_settings; then
    PYTEST_EXIT=1
    echo "✗ Failed to resolve managed FalkorDB settings for pytest"
    echo "  Run gobby install to configure the managed FalkorDB host, port, and password."
    FAILED=1
elif ! command -v agy >/dev/null 2>&1; then
    PYTEST_EXIT=1
    echo "✗ agy not found on PATH; install the AGY CLI before running pytest"
    FAILED=1
elif DATABASE_URL="$PYTEST_DATABASE_URL" \
    GOBBY_POSTGRES_TEST_DSN="$PYTEST_DATABASE_URL" \
    GOBBY_FALKORDB_HOST="$PYTEST_FALKORDB_HOST" \
    GOBBY_FALKORDB_PORT="$PYTEST_FALKORDB_PORT" \
    GOBBY_FALKORDB_PASSWORD="$PYTEST_FALKORDB_PASSWORD" \
    GOBBY_TEST_FALKOR_HOST="$PYTEST_FALKORDB_HOST" \
    GOBBY_TEST_FALKOR_PORT="$PYTEST_FALKORDB_PORT" \
    GOBBY_TEST_FALKOR_PASSWORD="$PYTEST_FALKORDB_PASSWORD" \
    GOBBY_RUN_AGY_PROBE=1 \
    GOBBY_RUN_POSTGRES_TMPFS_FILL_TEST=1 \
    GOBBY_OTEL_COLLECTOR_SMOKE=1 \
    GOBBY_GROK_AUDIT_TRANSCRIPTS_DIR="$PWD/tests/sessions/transcripts/fixtures/grok_audit" \
    GOBBY_TEST_PROTECT=1 \
    HOME="$PYTEST_ISOLATION_DIR/home" \
    GOBBY_HOME="$PYTEST_ISOLATION_DIR/gobby-home" \
    GOBBY_DATABASE_PATH="$PYTEST_ISOLATION_DIR/test.db" \
    GOBBY_CONFIG_FILE="$PYTEST_ISOLATION_DIR/config-test.yaml" \
    GOBBY_HOOKS_DIR="$PYTEST_ISOLATION_DIR/hooks" \
    GOBBY_LOGGING_DIR="$PYTEST_ISOLATION_DIR/logs" \
    uv_run pytest "${PYTEST_SELECTION_ARGS[@]}" -v --tb=line -rFEsw --cov=gobby --cov-report=term-missing --cov-fail-under=80 2>&1 | timestamp | tee "$PYTEST_REPORT"; then
    if check_pytest_postgres_skip_guard "$PYTEST_REPORT"; then
        echo "✓ Pytest passed"
    else
        PYTEST_EXIT=1
        echo "✗ Pytest failed"
        FAILED=1
    fi
else
    PYTEST_EXIT=$?
    echo "✗ Pytest failed"
    check_pytest_postgres_skip_guard "$PYTEST_REPORT" || true
    FAILED=1
fi
record_command_result "pytest" "$PYTEST_EXIT" "$PYTEST_REPORT"
echo ""

# CodeRabbit - AI code review report (informational, not a gate)
echo ">>> Running coderabbit review..."
if command -v coderabbit &> /dev/null; then
    CODERABBIT_REPORT="$REPORTS_DIR/coderabbit-$TIMESTAMP.md"
    CODERABBIT_EXIT=0
    if coderabbit review --agent --type all > "$CODERABBIT_REPORT" 2>&1; then
        echo "✓ CodeRabbit report saved to $CODERABBIT_REPORT"
    else
        CODERABBIT_EXIT=$?
        echo "✗ CodeRabbit review failed; diagnostics saved to $CODERABBIT_REPORT"
    fi
    record_command_result \
        "coderabbit" \
        "$CODERABBIT_EXIT" \
        "$CODERABBIT_REPORT" \
        "non-gating"
else
    echo "⊘ CodeRabbit not installed, skipping"
    record_skipped_command "coderabbit" "non-gating"
fi
echo ""

# Summary
echo "=== Summary ==="
echo "Reports saved to: $REPORTS_DIR/*-$TIMESTAMP.*"
echo "Manifest saved to: $MANIFEST_PATH"

REQUESTED_STATUS="passed"
if [ "$FAILED" -ne 0 ]; then
    REQUESTED_STATUS="failed"
fi
FINALIZE_EXIT=0
if python3 "$MANIFEST_TOOL" finish \
    --manifest "$MANIFEST_PATH" \
    --status "$REQUESTED_STATUS"; then
    MANIFEST_FINALIZED=1
else
    FINALIZE_EXIT=$?
    MANIFEST_FINALIZED=1
    FAILED=1
fi

if [ "$FINALIZE_EXIT" -eq 2 ]; then
    echo "✗ Source changed during the pre-push run; report manifest invalidated"
elif [ "$FINALIZE_EXIT" -ne 0 ]; then
    echo "✗ Failed to finalize the pre-push report manifest"
fi

if [ "$FAILED" -eq 0 ]; then
    echo "✓ All checks passed!"
    exit 0
else
    echo "✗ Some checks failed - review reports"
    exit 1
fi
