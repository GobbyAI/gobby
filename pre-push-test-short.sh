#!/usr/bin/env bash
set -uo pipefail

# Pre-push CI/CD test suite (no pytest)
# Runs linting, type checking, security scanning

TIMESTAMP=$(date +%s)
REPORTS_DIR="./reports"
mkdir -p "$REPORTS_DIR"
rm -f "$REPORTS_DIR"/*.txt "$REPORTS_DIR"/*.md "$REPORTS_DIR"/*.log 2>/dev/null || true

echo "=== Pre-push Test Suite ==="
echo "Timestamp: $TIMESTAMP"
echo ""

# Track failures
FAILED=0
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

uv_run() {
    if [ "${#UV_EXTRA_FLAGS[@]}" -gt 0 ]; then
        uv run "${UV_EXTRA_FLAGS[@]}" "$@"
    else
        uv run "$@"
    fi
}

# Ruff - autofix safe changes only (no unsafe fixes)
echo ">>> Running ruff check + format..."
uv_run ruff check src/ --fix --no-unsafe-fixes 2>&1 | tee "$REPORTS_DIR/ruff-$TIMESTAMP.txt"
ruff_status=${PIPESTATUS[0]}
if [ "$ruff_status" -eq 0 ]; then
    uv_run ruff format src/
    format_status=$?
    if [ "$format_status" -eq 0 ]; then
        echo "✓ Ruff passed"
    else
        echo "✗ Ruff format failed"
        FAILED=$((FAILED+1))
    fi
else
    echo "✗ Ruff check failed"
    FAILED=$((FAILED+1))
fi
echo ""

# Mypy - strict mode
echo ">>> Running mypy (strict)..."
uv_run mypy src/ --strict 2>&1 | tee "$REPORTS_DIR/mypy-$TIMESTAMP.txt"
mypy_status=${PIPESTATUS[0]}
if [ "$mypy_status" -eq 0 ]; then
    echo "✓ Mypy passed"
else
    echo "✗ Mypy failed"
    FAILED=$((FAILED+1))
fi
echo ""

# Python suppression ratchet
echo ">>> Checking Python suppression debt..."
uv_run gobby test-types suppressions . \
    --baseline .gobby/python-suppressions-baseline.json \
    2>&1 | tee "$REPORTS_DIR/python-suppressions-$TIMESTAMP.txt"
suppressions_status=${PIPESTATUS[0]}
if [ "$suppressions_status" -eq 0 ]; then
    echo "✓ Python suppression ratchet passed"
else
    echo "✗ Python suppression ratchet failed"
    FAILED=$((FAILED+1))
fi
echo ""

# Prettier - frontend formatting
echo ">>> Checking frontend formatting..."
(cd web && npm run format:check) 2>&1 | tee "$REPORTS_DIR/frontend-format-$TIMESTAMP.txt"
frontend_format_status=${PIPESTATUS[0]}
if [ "$frontend_format_status" -eq 0 ]; then
    echo "✓ Frontend format check passed"
else
    echo "✗ Frontend format check failed"
    FAILED=$((FAILED+1))
fi
echo ""

# TypeScript - frontend type checking
echo ">>> Running TypeScript check..."
(cd web && npx tsc --noEmit) 2>&1 | tee "$REPORTS_DIR/tsc-$TIMESTAMP.txt"
tsc_status=${PIPESTATUS[0]}
if [ "$tsc_status" -eq 0 ]; then
    echo "✓ TypeScript passed"
else
    echo "✗ TypeScript failed"
    FAILED=$((FAILED+1))
fi
echo ""

# ESLint - frontend linting
echo ">>> Running frontend lint..."
(cd web && npm run lint) 2>&1 | tee "$REPORTS_DIR/eslint-$TIMESTAMP.txt"
eslint_status=${PIPESTATUS[0]}
if [ "$eslint_status" -eq 0 ]; then
    echo "✓ Frontend lint passed"
else
    echo "✗ Frontend lint failed"
    FAILED=$((FAILED+1))
fi
echo ""

# Bandit - security linting
echo ">>> Running bandit..."
uv_run bandit -c pyproject.toml -r src/ -q 2>&1 | tee "$REPORTS_DIR/bandit-$TIMESTAMP.txt"
bandit_status=${PIPESTATUS[0]}
if [ "$bandit_status" -eq 0 ]; then
    echo "✓ Bandit passed"
else
    echo "✗ Bandit failed"
    FAILED=$((FAILED+1))
fi
echo ""

# pip-audit - dependency CVE scanning
echo ">>> Running pip-audit..."
uv_run pip-audit "${PIP_AUDIT_IGNORE_ARGS[@]}" 2>&1 | tee "$REPORTS_DIR/pip-audit-$TIMESTAMP.txt"
pipaudit_status=${PIPESTATUS[0]}
if [ "$pipaudit_status" -eq 0 ]; then
    echo "✓ pip-audit passed"
else
    echo "✗ pip-audit failed"
    FAILED=$((FAILED+1))
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
