#!/bin/bash
# net.sh: run a print-mode turn while sampling the network connections and open files of
# the agy process tree every 0.3 s; then reverse-resolve the remote IPs, list ~/.gemini
# files and directories newer than a marker, count URL hosts in the CLI log, and list the
# keychains in the search list. Record 1.1.9 (print half).
set -u
# shellcheck source=common.sh
. "$(dirname "$0")/common.sh"
OUT="$(run_dir netfs)"
cd "$GATE0_WORKSPACE" || exit 1
touch "$OUT/marker"
sleep 1
agy -p 'run: echo netprobe' "${AGY_FLAGS[@]}" >"$OUT/stdout" 2>"$OUT/stderr" &
PID=$!
: >"$OUT/lsof-net"
: >"$OUT/lsof-files"
while kill -0 "$PID" 2>/dev/null; do
	PIDS="$PID $(pgrep -P "$PID" | tr '\n' ' ')"
	for p in $PIDS; do
		lsof -nP -a -i -p "$p" 2>/dev/null | awk 'NR>1{print $9}' >>"$OUT/lsof-net"
		lsof -nP -p "$p" 2>/dev/null | awk 'NR>1 && $4!="txt" {print $9}' | awk '/\.gemini|\.antigravity|Keychains|\/tmp|\/private/' >>"$OUT/lsof-files"
	done
	sleep 0.3
done
wait "$PID"
echo "exit $?" >"$OUT/report"
sort -u "$OUT/lsof-net" | awk -F'->' '{print $2}' | awk -F: 'NF>1{print $1}' | sort -u >"$OUT/remote-ips"
while IFS= read -r ip; do
	printf '%s %s\n' "$ip" "$(dig +short -x "$ip" 2>/dev/null | tr '\n' ' ')"
done <"$OUT/remote-ips" >"$OUT/remote-hosts"
sort -u "$OUT/lsof-files" >"$OUT/open-files"
find "$HOME/.gemini" -newer "$OUT/marker" -type f 2>/dev/null | sort >"$OUT/written-files"
find "$HOME/.gemini" -newer "$OUT/marker" -type d 2>/dev/null | sort >"$OUT/written-dirs"
# shellcheck disable=SC2012  # log names are fixed-format cli-<timestamp>.log
L=$(ls -t "$HOME/.gemini/antigravity-cli/log/cli-"*.log | head -1)
awk '/URL: https?:\/\//{for(i=1;i<=NF;i++) if ($i ~ /^https?:\/\//) print $i}' "$L" | awk -F/ '{print $3}' | sort | uniq -c >"$OUT/log-hosts"
security list-keychains >"$OUT/keychains" 2>&1
for s in report remote-hosts log-hosts open-files written-files written-dirs keychains stdout stderr; do
	section "$s" "$OUT/$s"
done
