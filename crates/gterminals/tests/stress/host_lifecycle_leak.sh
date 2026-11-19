#!/bin/sh
# Stress-samples the host_lifecycle LEAK-FAIL flake (#24228): explicit_drain_uses_existing_host_shutdown_wire
# passes its body, then nextest reports leaked handles after the 10s leak-timeout. Sweeps of 20 and 60
# clean runs have not reproduced it; the observed rate is about one leak per 86 full-binary runs.
#
# Runs the host_lifecycle tests in the suite binary up to ITERATIONS times under nextest and stops at the
# first LEAK-FAIL (exit 1). Any other failing run exits 2. A clean sweep exits 0.
#
# Usage: host_lifecycle_leak.sh [iterations] [test-threads]
# Run from the repository root. DATABASE_URL must name the isolated test hub
# (gobby_test on 127.0.0.1:60892); the script refuses to run without it.
set -u

iterations=${1:-20}
threads=${2:-9}
if [ -z "${DATABASE_URL:-}" ]; then
    echo "DATABASE_URL must name the isolated test hub" >&2
    exit 2
fi

run=1
while [ "$run" -le "$iterations" ]; do
    out=$(cargo nextest run -p gobby-terminals --test suite -E 'test(/^host_lifecycle::/)' \
        --no-fail-fast --test-threads "$threads" 2>&1)
    status=$?
    id=$(printf '%s\n' "$out" | sed -n 's/.*Nextest run ID \([0-9a-f-]*\).*/\1/p' | head -n 1)
    if printf '%s\n' "$out" | grep -q 'LEAK-FAIL'; then
        echo "LEAK run=$run nextest_run_id=$id threads=$threads"
        printf '%s\n' "$out" | grep 'LEAK-FAIL' | sort -u
        exit 1
    fi
    if [ "$status" -ne 0 ]; then
        echo "FAIL run=$run nextest_run_id=$id status=$status (not a leak)"
        exit 2
    fi
    echo "ok run=$run nextest_run_id=$id"
    run=$((run + 1))
done
echo "no leak in $iterations runs (threads=$threads)"
exit 0
