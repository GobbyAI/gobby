#!/bin/bash
# cancel.sh <INT|TERM> [CONVERSATION_ID]: start a long print-mode turn in the background,
# wait for the ACTIVE tool step, list the process tree, signal agy, and list orphans
# after 2 s and 45 s. Record 1.1.8 (print half).
set -u
SIG="$1"
CONV="${2:-}"
# shellcheck source=common.sh
. "$(dirname "$0")/common.sh"
OUT="$(run_dir "cancel-$SIG")"
cd "$GATE0_WORKSPACE" || exit 1
EXTRA=()
[ -n "$CONV" ] && EXTRA=(--conversation "$CONV")
agy -p 'run: sleep 40; echo finished-after-sleep' "${AGY_FLAGS[@]}" "${EXTRA[@]}" >"$OUT/stdout" 2>"$OUT/stderr" &
PID=$!
echo "agy pid $PID" >"$OUT/report"
# wait until the tool step is ACTIVE (the sleep child exists)
for i in $(seq 1 40); do
	if awk '/"state":"ACTIVE","step_type":"tool"/{f=1} END{exit !f}' "$OUT/stdout"; then
		sleep 2
		break
	fi
	sleep 1
done
{
	echo "--- before signal (t=${i}s)"
	echo "children of agy:"
	pgrep -lP "$PID"
	echo "sleep procs:"
	ps -axo pid,ppid,stat,command | awk '$4=="sleep"'
	echo "process tree (agy/antigravity-cli):"
	ps -axo pid,ppid,stat,command | awk '/agy -p|antigravity-cli/ && !/awk/' | cut -c1-160
} >>"$OUT/report"
kill -"$SIG" "$PID"
T0=$(date +%s)
wait "$PID"
RC=$?
T1=$(date +%s)
{
	echo "--- sent SIG$SIG; exit code $RC after $((T1 - T0))s"
	sleep 2
	echo "orphans after 2s:"
	ps -axo pid,ppid,stat,command | awk '$4=="sleep" || (/agy -p|antigravity-cli/ && !/awk/)' | cut -c1-160
	echo "stdout lines: $(wc -l <"$OUT/stdout")"
	echo "stdout tail:"
	tail -3 "$OUT/stdout" | cut -c1-400
	echo "stderr:"
	tail -3 "$OUT/stderr" | cut -c1-300
} >>"$OUT/report"
sleep 45
{
	echo "--- after 45s: sleep still present?"
	ps -axo pid,ppid,stat,command | awk '$4=="sleep"'
	echo "done"
} >>"$OUT/report"
section report "$OUT/report"
section stdout "$OUT/stdout"
section stderr "$OUT/stderr"
