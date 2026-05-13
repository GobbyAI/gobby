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
rm -f "$REPORTS_DIR"/*.txt "$REPORTS_DIR"/*.md "$REPORTS_DIR"/*.log 2>/dev/null || true

echo "=== Pre-push Test Suite ==="
echo "Timestamp: $TIMESTAMP"
echo ""

# Track failures
FAILED=0
PYTEST_ISOLATION_DIR=""

cleanup() {
    if [ -n "${PYTEST_ISOLATION_DIR:-}" ] && [ -d "$PYTEST_ISOLATION_DIR" ]; then
        rm -rf "$PYTEST_ISOLATION_DIR"
    fi
}
trap cleanup EXIT

# Ruff - autofix safe changes only (no unsafe fixes)
echo ">>> Running ruff check + format..."
if uv run ruff check src/ --fix --no-unsafe-fixes 2>&1 | tee "$REPORTS_DIR/ruff-$TIMESTAMP.txt"; then
    uv run ruff format src/
    echo "✓ Ruff passed"
else
    echo "✗ Ruff failed"
    FAILED=1
fi
echo ""

# Mypy - strict mode
echo ">>> Running mypy (strict)..."
if uv run mypy src/ --strict --no-incremental 2>&1 | tee "$REPORTS_DIR/mypy-$TIMESTAMP.txt"; then
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
if uv run bandit -c pyproject.toml -r src/ -q 2>&1 | tee "$REPORTS_DIR/bandit-$TIMESTAMP.txt"; then
    echo "✓ Bandit passed"
else
    echo "✗ Bandit failed"
    FAILED=1
fi
echo ""

# pip-audit - dependency CVE scanning
echo ">>> Running pip-audit..."
if uv run pip-audit 2>&1 | tee "$REPORTS_DIR/pip-audit-$TIMESTAMP.txt"; then
    echo "✓ pip-audit passed"
else
    echo "✗ pip-audit failed"
    FAILED=1
fi
echo ""

# Test quality - static audit against tracked baseline
echo ">>> Running test-quality audit..."
if uv run gobby test-quality audit --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high 2>&1 | tee "$REPORTS_DIR/test-quality-$TIMESTAMP.txt"; then
    echo "✓ Test-quality audit passed"
else
    echo "✗ Test-quality audit failed"
    FAILED=1
fi
echo ""

# Pytest - tests with coverage (80% threshold)
# Uses verbose mode with timestamps to correlate test execution with daemon logs
echo ">>> Running pytest with coverage..."
PYTEST_ISOLATION_DIR=$(mktemp -d "${TMPDIR:-/tmp}/gobby-pre-push-${TIMESTAMP}.XXXXXX")
if ! mkdir -p \
    "$PYTEST_ISOLATION_DIR/home" \
    "$PYTEST_ISOLATION_DIR/gobby-home" \
    "$PYTEST_ISOLATION_DIR/logs" \
    "$PYTEST_ISOLATION_DIR/hooks"; then
    echo "✗ Failed to create pytest isolation directories under PYTEST_ISOLATION_DIR=$PYTEST_ISOLATION_DIR"
    echo "  Check directory permissions and available disk space."
    FAILED=1
elif GOBBY_TEST_PROTECT=1 \
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
    uv run pytest -v --tb=line -rFEw --cov=gobby --cov-fail-under=80 --cov-report=term-missing 2>&1 | timestamp | tee "$REPORTS_DIR/pytest-$TIMESTAMP.txt"; then
    echo "✓ Pytest passed"
else
    echo "✗ Pytest failed"
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
