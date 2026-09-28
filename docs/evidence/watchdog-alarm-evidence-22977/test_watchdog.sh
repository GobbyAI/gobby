#!/bin/bash
# Fixture tests for watchdog.sh alarm evidence. Runs the script under a temporary
# HOME with a fake `gobby` that records the alert instead of sending it, and the
# isolated test hub as its read-only database. Usage: test_watchdog.sh <script>
set -u

script=${1:?usage: test_watchdog.sh <watchdog.sh>}
fails=0
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

check() {
  local name=$1
  shift
  if "$@"; then echo "ok   $name"; else echo "FAIL $name"; fails=$((fails + 1)); fi
}

# Build an isolated HOME. $1 = name, stdin = errors.log body, $2 = prior line count.
make_home() {
  local home="$tmp/$1"
  mkdir -p "$home/.gobby/watchdog" "$home/.gobby/logs" "$home/.local/bin" "$home/Projects/gobby"
  cp "$script" "$home/.gobby/watchdog/watchdog.sh"
  cat >"$home/.gobby/logs/errors.log"
  : >"$home/.gobby/logs/daemon.log"
  printf 'database_url: postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test\n' \
    >"$home/.gobby/bootstrap.yaml"
  printf '%s 0 none 0\n' "$2" >"$home/.gobby/watchdog/state"
  cat >"$home/.local/bin/gobby" <<EOF
#!/bin/bash
printf '%s' "\$4" >"$home/sent.txt"
echo "Message sent to \$3"
EOF
  chmod +x "$home/.local/bin/gobby"
  echo "$home"
}

run() { HOME="$1" bash "$1/.gobby/watchdog/watchdog.sh" 2>"$1/stderr.txt"; }

s1=9558f41b-2911-44f1-b6f3-05926322acfc
s2=685d6860-d154-4b1d-b55c-e62b3c92ace7
s3=0985316e-f428-4757-b15e-32a17549112e
s4=26de7dbf-1f31-455c-ade4-993cc7c42cf6
s5=11111111-2222-4333-8444-555555555555

tb() {
  printf 'Traceback (most recent call last):\n'
  printf '  File "/x/host_client.py", line 282, in _begin_request\n'
  printf '    raise HostConnectionLost("control closed")\n'
  printf '%s\n' "$1"
}

# --- alarm fixture: 5 old lines, then the new window ---
home=$(
  {
    for i in 1 2 3 4 5; do echo "2026-09-27 17:00:0$i - INFO     - old.line - baseline $i"; done
    # Orphan tail of an entry that began before the window, naming a bad string.
    echo "  File \"/x/hub.py\", line 9, in epoch"
    echo "gobby.storage.hub.HostEpochChangedError: epoch moved"
    for s in $s1 $s2 $s4 $s5; do
      echo "2026-09-27 17:21:54 - WARNING  - agents.tmux.pane_monitor._check_attention_panes - TmuxPaneMonitor: failed to capture terminal for session $s"
      tb "gobby.terminals.host_client.HostConnectionLost: host_unavailable"
    done
    echo "2026-09-27 17:22:10 - ERROR    - mcp_proxy.tools.sessions._confirm - Session $s3 never showed Confirm"
    tb "TimeoutError: prompt never appeared"
    echo "2026-09-27 17:23:00 - ERROR    - storage.pool.acquire - pool acquisition failed after 5s"
    echo "2026-09-27 17:24:00 - ERROR    - memory.x.y - unrelated failure"
    tb "ValueError: bad"
    echo "2026-09-27 17:25:00 - WARNING  - mcp_proxy.results.search_tool_result - Failed to search stored tool result r1"
    tb "KeyError: r1"
    echo "2026-09-27 17:26:00 - WARNING  - servers.slow - Slow hook execution without traceback"
  } | make_home alarm 5
)
run "$home"
sent="$home/sent.txt"
body=$(cat "$sent" 2>/dev/null)
new_lines=$(($(wc -l <"$home/.gobby/logs/errors.log") - 5))

check "alert was sent through the fake gobby" test -s "$sent"
check "physical log-line delta is present" grep -q "^errors.log: $((new_lines + 5)) lines (+$new_lines)" "$sent"
check "event and signature counts are distinct from line delta" \
  grep -q "^ALARM\[errors\]: 8 alarm events, 5 signatures, in +$new_lines new errors.log lines" "$sent"
check "top signature is the 4x pane-monitor warning with time" \
  grep -q "^• 4x 17:21:54 agents.tmux.pane_monitor._check_attention_panes - TmuxPaneMonitor: failed to capture terminal for session <id>" "$sent"
check "exception context line is present" \
  grep -q "^   exception: gobby.terminals.host_client.HostConnectionLost: host_unavailable" "$sent"
check "session ids are listed, capped at 3 with overflow" \
  grep -q "^   sessions: $s1, $s2, $s4 (+1 more)" "$sent"
check "orphan bad-string line counts as an event" grep -q "HostEpochChangedError" "$sent"
check "only three signatures are expanded" test "$(grep -c '^• ' "$sent")" -eq 3
check "remaining signatures are summarized" grep -q "^(+2 more signatures)" "$sent"
check "benign search_tool_result traceback is excluded" \
  test "$(grep -c 'search_tool_result' "$sent")" -eq 0
check "no local last.txt path in the alert" test "$(grep -c 'last.txt' "$sent")" -eq 0
check "alert fits Telegram's 4096-char limit" test "${#body}" -lt 4096
check "last.txt still written" grep -q "^errors.log:" "$home/.gobby/watchdog/last.txt"

# --- quiet fixture: new lines but nothing alarm-worthy ---
home=$(
  {
    for i in 1 2 3; do echo "2026-09-27 17:00:0$i - INFO     - old.line - baseline $i"; done
    echo "2026-09-27 17:26:00 - WARNING  - servers.slow - Slow hook execution without traceback"
  } | make_home quiet 3
)
run "$home"
check "quiet run reports no errors alarm" \
  test "$(grep -c 'ALARM\[errors\]' "$home/.gobby/watchdog/last.txt")" -eq 0
check "quiet run keeps the physical delta in last.txt" \
  grep -q "^errors.log: 4 lines (+1)" "$home/.gobby/watchdog/last.txt"
check "state keeps its four-field format" \
  grep -Eq '^[0-9]+ [0-9]+ [a-z,]+ [0-9]+$' "$home/.gobby/watchdog/state"

echo "failures: $fails"
[ "$fails" -eq 0 ]
