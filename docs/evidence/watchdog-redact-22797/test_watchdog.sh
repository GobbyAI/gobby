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
  # Fake CLI: `comms attach` records its args and stdin; `comms send` records the
  # alert, or fails when $home/fail-send exists.
  cat >"$home/.local/bin/gobby" <<EOF
#!/bin/bash
if [ "\$2" = attach ]; then
  printf '%s\n' "\$@" >"$home/attach-args.txt"
  cat >"$home/attached.txt"
  echo "attached"
  exit 0
fi
[ -f "$home/fail-send" ] && exit 1
printf '%s\n' "\$@" >"$home/args.txt"
printf '%s' "\${@: -1}" >"$home/sent.txt"
echo "Message sent"
EOF
  # Fake osascript so the fallback never raises a real macOS notification.
  mkdir -p "$home/bin"
  # Keep load fixtures independent of the host's current load.
  printf '0 0 0\n' >"$home/load.txt"
  cat >"$home/bin/sysctl" <<EOF
#!/bin/bash
printf '{ %s }\\n' "\$(cat "$home/load.txt")"
EOF
  printf '#!/bin/bash\ntouch "%s/osascript-called"\n' "$home" >"$home/bin/osascript"
  chmod +x "$home/.local/bin/gobby" "$home/bin/osascript" "$home/bin/sysctl"
  echo "$home"
}

run() { HOME="$1" PATH="$1/bin:$PATH" bash "$1/.gobby/watchdog/watchdog.sh" 2>"$1/stderr.txt"; }

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
check "alert is sent with --redact to gobby-telegram" \
  test "$(sed -n '3,4p' "$home/args.txt" | paste -sd' ' -)" = "--redact gobby-telegram"
check "physical log-line delta is present" grep -q "^errors.log: $((new_lines + 5)) lines (+$new_lines)" "$sent"
check "event and signature counts are distinct from line delta" \
  grep -q "^ALARM\[errors\]: 8 alarm events, 5 signatures, in +$new_lines new errors.log lines" "$sent"
check "top signature is the 4x pane-monitor warning with time" \
  grep -q "^• 4x 17:21:54 agents.tmux.pane_monitor._check_attention_panes - TmuxPaneMonitor: failed to capture terminal for session <id>" "$sent"
check "no exception line quoted in the alert" test "$(grep -c 'exception:\|HostConnectionLost' "$sent")" -eq 0
check "no session ids quoted in the alert" test "$(grep -c "$s1\|$s2\|$s3\|$s4\|$s5" "$sent")" -eq 0
check "no traceback text in the alert" test "$(grep -c 'Traceback\|File \"' "$sent")" -eq 0
check "orphan bad-string line counts as an event" grep -q "HostEpochChangedError" "$sent"
check "new lines are attached as errors-new.txt to gobby-telegram" \
  test "$(paste -sd' ' - <"$home/attach-args.txt")" = \
  "comms attach --caption errors.log new lines, redacted gobby-telegram errors-new.txt"
check "attached content is exactly the new errors.log window" \
  cmp -s "$home/attached.txt" <(tail -n "$new_lines" "$home/.gobby/logs/errors.log")
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
check "quiet run attaches nothing" test ! -e "$home/attached.txt"

# --- path fixture: host paths in a header never reach the alert text ---
home=$(
  {
    echo "2026-09-27 17:00:01 - INFO     - old.line - baseline"
    echo "2026-09-27 17:25:30 - ERROR    - storage.files.open - failed to open /tmp/errors.log, '/Users/someone/.gobby/x.db' (cwd=~/private/dir) cwd:/tmp/a url=file:///var/b [/opt/c]"
    tb "OSError: denied"
  } | make_home paths 1
)
run "$home"
sent="$home/sent.txt"
check "path signature is sent" \
  grep -Fqx "• 1x 17:25:30 storage.files.open - failed to open <path>, '<path>' (cwd=<path>) cwd:<path> url=file:<path> [<path>]" "$sent"
check "no host path in the alert" test "$(grep -c '/tmp/\|/Users/\|/var/\|/opt/\|~/' "$sent")" -eq 0

# --- failed-send fixture: Telegram down, so no attachment follows the fallback ---
home=$(
  {
    echo "2026-09-27 17:00:01 - INFO     - old.line - baseline"
    echo "2026-09-27 17:23:00 - ERROR    - storage.pool.acquire - pool acquisition failed after 5s"
  } | make_home failsend 1
)
touch "$home/fail-send"
run "$home"
check "failed send falls back to osascript" test -e "$home/osascript-called"
check "failed send attaches nothing" test ! -e "$home/attached.txt"

# --- load fixtures: only the five-minute reading above 30 raises a load alarm ---
fixture_home=$(printf 'baseline\n' | make_home load-above 1)
printf '1 30.01 1\n' >"$fixture_home/load.txt"
run "$fixture_home"
check "five-minute load above 30 alarms with the correct text" \
  grep -Fqx 'ALARM[load]: 5-min load 30.01 > 30' "$fixture_home/.gobby/watchdog/last.txt"

fixture_home=$(printf 'baseline\n' | make_home load-boundary 1)
printf '1 30 1\n' >"$fixture_home/load.txt"
run "$fixture_home"
check "five-minute load exactly 30 does not alarm" \
  test "$(grep -c '^ALARM\[load\]' "$fixture_home/.gobby/watchdog/last.txt")" -eq 0

fixture_home=$(printf 'baseline\n' | make_home load-one-minute 1)
printf '40 29 1\n' >"$fixture_home/load.txt"
run "$fixture_home"
check "one-minute spike above 30 does not alarm when five-minute load is below 30" \
  test "$(grep -c '^ALARM\[load\]' "$fixture_home/.gobby/watchdog/last.txt")" -eq 0

echo "failures: $fails"
[ "$fails" -eq 0 ]
