#!/bin/bash
# Shared setup for the Gate 0 shell probes: workspace, run directory, agy launch flags.
GATE0_WORKSPACE="${GATE0_WORKSPACE:-$(mktemp -d "${TMPDIR:-/tmp}/agy-gate0-ws.XXXXXX")}"
GATE0_RUNS="${GATE0_RUNS:-$(mktemp -d "${TMPDIR:-/tmp}/agy-gate0-runs.XXXXXX")}"
mkdir -p "$GATE0_WORKSPACE" "$GATE0_RUNS"
AGY_MODEL="gpt-oss-120b-medium"
# shellcheck disable=SC2034  # consumed by the sourcing probes
AGY_FLAGS=(--output-format stream-json --sandbox=false --dangerously-skip-permissions --add-dir "$GATE0_WORKSPACE" --print-timeout 3m --model "$AGY_MODEL")

run_dir() {
	# run_dir <name>: a fresh, empty run directory under GATE0_RUNS.
	local dir="$GATE0_RUNS/$1"
	[ -e "$dir" ] && dir="$(mktemp -d "$GATE0_RUNS/$1.XXXXXX")"
	mkdir -p "$dir"
	printf '%s' "$dir"
}

section() {
	# section <title> <file>: print "--- title ---" then the file's contents.
	printf -- '--- %s ---\n' "$1"
	cat "$2"
}
