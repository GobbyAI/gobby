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
mkdir -p "$REPORTS_DIR"
rm -f "$REPORTS_DIR"/*.txt "$REPORTS_DIR"/*.md "$REPORTS_DIR"/*.log "$REPORTS_DIR"/*.json 2>/dev/null || true

echo "=== Pre-push Test Suite ==="
echo "Timestamp: $TIMESTAMP"
echo ""

# Track failures
FAILED=0
PYTEST_ISOLATION_DIR=""
UV_EXTRA_FLAGS=()
if [ "${GOBBY_UV_ALL_EXTRAS:-}" = "1" ]; then
    UV_EXTRA_FLAGS=(--all-extras)
fi
POSTGRES_SKIP_REASONS=(
    "DATABASE_URL or configured bootstrap database_url is required"
    "PostgreSQL DSN required for hub runtime surface tests"
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
    uv run "${UV_EXTRA_FLAGS[@]}" "$@"
}

cleanup() {
    if [ -n "${PYTEST_ISOLATION_DIR:-}" ] && [ -d "$PYTEST_ISOLATION_DIR" ]; then
        rm -rf "$PYTEST_ISOLATION_DIR"
    fi
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

read_bootstrap_database_url() {
    uv_run python - <<'PY'
from gobby.config.bootstrap import BootstrapConfigError, load_bootstrap

try:
    database_url = load_bootstrap(resolve_database_url=True).database_url
except BootstrapConfigError:
    database_url = None

if database_url:
    print(database_url)
PY
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
            printf 'postgresql://%s:%s@localhost:%s/%s\n' \
                "${GOBBY_POSTGRES_TEST_USER:-gobby_test}" \
                "${GOBBY_POSTGRES_TEST_PASSWORD:-gobby_test}" \
                "${GOBBY_POSTGRES_TEST_PORT:-60892}" \
                "${GOBBY_POSTGRES_TEST_DB:-gobby_test}"
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

resolve_pytest_database_url() {
    if [ -n "${DATABASE_URL:-}" ]; then
        printf '%s\n' "$DATABASE_URL"
        return 0
    fi

    local bootstrap_database_url
    bootstrap_database_url=$(read_bootstrap_database_url 2>/dev/null || true)
    if [ -n "$bootstrap_database_url" ]; then
        printf '%s\n' "$bootstrap_database_url"
        return 0
    fi

    start_docker_postgres_test_database
}

check_pytest_postgres_skip_guard() {
    local report_path="$1"
    local reason
    for reason in "${POSTGRES_SKIP_REASONS[@]}"; do
        if [ -f "$report_path" ] && grep -q "$reason" "$report_path"; then
            echo "✗ Pytest skipped PostgreSQL tests because no database URL was available"
            echo "  Report contains: $reason"
            return 1
        fi
    done
    return 0
}

# Ruff - autofix safe changes only (no unsafe fixes)
echo ">>> Running ruff check + format..."
if uv_run ruff check src/ --fix --no-unsafe-fixes 2>&1 | tee "$REPORTS_DIR/ruff-$TIMESTAMP.txt"; then
    uv_run ruff format src/
    echo "✓ Ruff passed"
else
    echo "✗ Ruff failed"
    FAILED=1
fi
echo ""

# Mypy - strict mode
echo ">>> Running mypy (strict)..."
if uv_run mypy src/ --strict --no-incremental 2>&1 | tee "$REPORTS_DIR/mypy-$TIMESTAMP.txt"; then
    echo "✓ Mypy passed"
else
    echo "✗ Mypy failed"
    FAILED=1
fi
echo ""

# TypeScript - frontend type checking
echo ">>> Running TypeScript check..."
if (cd web && npx tsc --noEmit) 2>&1 | tee "$REPORTS_DIR/tsc-$TIMESTAMP.txt"; then
    echo "✓ TypeScript passed"
else
    echo "✗ TypeScript failed"
    FAILED=1
fi
echo ""

# ESLint - frontend linting
echo ">>> Running frontend lint..."
if (cd web && npm run lint) 2>&1 | tee "$REPORTS_DIR/eslint-$TIMESTAMP.txt"; then
    echo "✓ Frontend lint passed"
else
    echo "✗ Frontend lint failed"
    FAILED=1
fi
echo ""

# Vitest - frontend tests with coverage
echo ">>> Running vitest..."
if (cd web && npx vitest run --coverage) 2>&1 | tee "$REPORTS_DIR/vitest-$TIMESTAMP.txt"; then
    echo "✓ Vitest passed"
else
    echo "✗ Vitest failed"
    FAILED=1
fi
echo ""

# Bandit - security linting
echo ">>> Running bandit..."
if uv_run bandit -c pyproject.toml -r src/ -q 2>&1 | tee "$REPORTS_DIR/bandit-$TIMESTAMP.txt"; then
    echo "✓ Bandit passed"
else
    echo "✗ Bandit failed"
    FAILED=1
fi
echo ""

# pip-audit - dependency CVE scanning
echo ">>> Running pip-audit..."
if uv_run pip-audit 2>&1 | tee "$REPORTS_DIR/pip-audit-$TIMESTAMP.txt"; then
    echo "✓ pip-audit passed"
else
    echo "✗ pip-audit failed"
    FAILED=1
fi
echo ""

# Test quality - static audit against tracked baseline
echo ">>> Running test-quality audit..."
if uv_run gobby test-quality audit --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high 2>&1 | tee "$REPORTS_DIR/test-quality-$TIMESTAMP.txt"; then
    echo "✓ Test-quality audit passed"
else
    echo "✗ Test-quality audit failed"
    FAILED=1
fi
echo ""

# Pytest - tests with coverage report. Keep HOME/GOBBY_HOME isolated while
# passing an explicit PostgreSQL test target so DB coverage cannot silently skip.
# Uses verbose mode with timestamps to correlate test execution with daemon logs
echo ">>> Running pytest with coverage..."
PYTEST_ISOLATION_DIR=$(mktemp -d "${TMPDIR:-/tmp}/gobby-pre-push-${TIMESTAMP}.XXXXXX")
PYTEST_REPORT="$REPORTS_DIR/pytest-$TIMESTAMP.txt"
if ! PYTEST_DATABASE_URL=$(resolve_pytest_database_url); then
    echo "✗ Failed to resolve PostgreSQL DATABASE_URL for pytest"
    echo "  Set DATABASE_URL, configure ~/.gobby/bootstrap.yaml database_url, or enable Docker"
    echo "  so docker-compose.test.yml can start postgres-test on port 60892."
    FAILED=1
elif ! mkdir -p \
    "$PYTEST_ISOLATION_DIR/home" \
    "$PYTEST_ISOLATION_DIR/gobby-home" \
    "$PYTEST_ISOLATION_DIR/logs" \
    "$PYTEST_ISOLATION_DIR/hooks"; then
    echo "✗ Failed to create pytest isolation directories under PYTEST_ISOLATION_DIR=$PYTEST_ISOLATION_DIR"
    echo "  Check directory permissions and available disk space."
    FAILED=1
elif DATABASE_URL="$PYTEST_DATABASE_URL" \
    GOBBY_POSTGRES_TEST_DSN="$PYTEST_DATABASE_URL" \
    GOBBY_TEST_PROTECT=1 \
    HOME="$PYTEST_ISOLATION_DIR/home" \
    GOBBY_HOME="$PYTEST_ISOLATION_DIR/gobby-home" \
    GOBBY_DATABASE_PATH="$PYTEST_ISOLATION_DIR/test.db" \
    GOBBY_CONFIG_FILE="$PYTEST_ISOLATION_DIR/config-test.yaml" \
    GOBBY_HOOKS_DIR="$PYTEST_ISOLATION_DIR/hooks" \
    GOBBY_LOGGING_CLIENT="$PYTEST_ISOLATION_DIR/logs/gobby.log" \
    GOBBY_LOGGING_CLIENT_ERROR="$PYTEST_ISOLATION_DIR/logs/gobby-error.log" \
    GOBBY_LOGGING_MCP_SERVER="$PYTEST_ISOLATION_DIR/logs/mcp-server.log" \
    GOBBY_LOGGING_MCP_CLIENT="$PYTEST_ISOLATION_DIR/logs/mcp-client.log" \
    GOBBY_LOGGING_HOOK_MANAGER="$PYTEST_ISOLATION_DIR/logs/hook-manager.log" \
    uv_run pytest "${PYTEST_SELECTION_ARGS[@]}" -v --tb=line -rFEsw --cov=gobby --cov-report=term-missing --cov-fail-under=80 2>&1 | timestamp | tee "$PYTEST_REPORT"; then
    if check_pytest_postgres_skip_guard "$PYTEST_REPORT"; then
        echo "✓ Pytest passed"
    else
        echo "✗ Pytest failed"
        FAILED=1
    fi
else
    echo "✗ Pytest failed"
    check_pytest_postgres_skip_guard "$PYTEST_REPORT" || true
    FAILED=1
fi
echo ""

# CodeRabbit - AI code review report (informational, not a gate)
echo ">>> Running coderabbit review..."
if command -v coderabbit &> /dev/null; then
    coderabbit review --prompt-only --type all > "$REPORTS_DIR/coderabbit-$TIMESTAMP.md" 2>&1
    echo "✓ CodeRabbit report saved to $REPORTS_DIR/coderabbit-$TIMESTAMP.md"
else
    echo "⊘ CodeRabbit not installed, skipping"
fi
echo ""

# Summary
echo "=== Summary ==="
echo "Reports saved to: $REPORTS_DIR/*-$TIMESTAMP.txt"

if [ $FAILED -eq 0 ]; then
    echo "✓ All checks passed!"
    exit 0
else
    echo "✗ Some checks failed - review reports"
    exit 1
fi
