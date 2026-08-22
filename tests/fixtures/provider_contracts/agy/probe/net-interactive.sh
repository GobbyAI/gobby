#!/bin/bash
# net-interactive.sh [SECONDS]: sample the network connections, open files, and child
# processes of the interactive agy launched by the `agy-gate0` tmux session (plan §1.1
# terminal-mode mechanics) for SECONDS (default 120) while a `run: echo netprobe` turn
# runs, then reverse-resolve the remote IPs, list ~/.gemini files newer than a marker,
# and count URL hosts in the CLI log. Record 1.1.9 (interactive half).
set -u
# shellcheck source=common.sh
. "$(dirname "$0")/common.sh"
OUT="$(run_dir net-interactive)"
SECS="${1:-120}"
PID="$(pgrep -f "agy --sandbox=false --dangerously-skip-permissions --model $AGY_MODEL --add-dir" | head -1)"
echo "agy pid $PID" >"$OUT/report"
touch "$OUT/marker"
: >"$OUT/lsof-net"
: >"$OUT/lsof-files"
: >"$OUT/children"
end=$(($(date +%s) + SECS))
while [ "$(date +%s)" -lt "$end" ]; do
	PIDS="$PID $(pgrep -P "$PID" | tr '\n' ' ')"
	for p in $PIDS; do
		lsof -nP -a -i -p "$p" 2>/dev/null | awk 'NR>1{print $9}' >>"$OUT/lsof-net"
		lsof -nP -p "$p" 2>/dev/null | awk 'NR>1 && $4!="txt" {print $9}' | awk '/\.gemini|\.antigravity|Keychains|\/tmp|\/private/' >>"$OUT/lsof-files"
	done
	ps -o pid=,ppid=,command= -p "$(echo "$PIDS" | tr ' ' ',' | sed 's/,$//')" 2>/dev/null >>"$OUT/children"
	sleep 0.3
done
sort -u "$OUT/lsof-net" | awk -F'->' '{print $2}' | awk -F: 'NF>1{print $1}' | sort -u >"$OUT/remote-ips"
while IFS= read -r ip; do
	printf '%s %s\n' "$ip" "$(dig +short -x "$ip" 2>/dev/null | tr '\n' ' ')"
done <"$OUT/remote-ips" >"$OUT/remote-hosts"
sort -u "$OUT/lsof-files" >"$OUT/open-files"
sort -u "$OUT/children" >"$OUT/process-tree"
find "$HOME/.gemini" -newer "$OUT/marker" -type f 2>/dev/null | sort >"$OUT/written-files"
# shellcheck disable=SC2012  # log names are fixed-format cli-<timestamp>.log
L=$(ls -t "$HOME/.gemini/antigravity-cli/log/cli-"*.log | head -1)
awk '/URL: https?:\/\//{for(i=1;i<=NF;i++) if ($i ~ /^https?:\/\//) print $i}' "$L" | awk -F/ '{print $3}' | sort | uniq -c >"$OUT/log-hosts"
for s in report remote-hosts log-hosts process-tree open-files written-files; do
	section "$s" "$OUT/$s"
done
