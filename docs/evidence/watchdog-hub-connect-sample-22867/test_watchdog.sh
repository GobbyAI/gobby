#!/bin/bash
# Fixture tests for the watchdog's gcode-process and hub-connection samples (#22867).
# Runs the script under a temporary HOME with a fake `gobby` that records the alert
# instead of sending it, and the isolated test hub as its read-only database.
# Usage: test_watchdog.sh <watchdog.sh>
set -u

script=${1:?usage: test_watchdog.sh <watchdog.sh>}
fails=0
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# The test hub has no agent_runs table. This psql gives agent_runs queries an empty
# one and sends every query to the real hub, so the connection count is real and an
# unreachable hub still fails.
real_psql=/opt/homebrew/opt/libpq/bin/psql
cat >"$tmp/psql" <<EOF
#!/bin/bash
args=("\$@")
n=\${#args[@]}
q=\${args[n-1]}
case \$q in
  *agent_runs*) q="with agent_runs(id, agent_name, status) as (select null::uuid, null::text, null::text where false) \$q" ;;
esac
exec $real_psql "\${args[@]:0:n-1}" "\$q"
EOF
chmod +x "$tmp/psql"

check() {
  local name=$1
  shift
  if "$@"; then echo "ok   $name"; else echo "FAIL $name"; fails=$((fails + 1)); fi
}

# Build an isolated HOME. $1 = name, $2 = database_url, stdin = errors.log body.
make_home() {
  local home="$tmp/$1"
  mkdir -p "$home/.gobby/watchdog" "$home/.gobby/logs" "$home/.local/bin" "$home/Projects/gobby"
  sed "s|^psql=$real_psql\$|psql=$tmp/psql|" "$script" >"$home/.gobby/watchdog/watchdog.sh"
  cat >"$home/.gobby/logs/errors.log"
  : >"$home/.gobby/logs/daemon.log"
  printf 'database_url: %s\n' "$2" >"$home/.gobby/bootstrap.yaml"
  printf '1 0 none 0\n' >"$home/.gobby/watchdog/state"
  cat >"$home/.local/bin/gobby" <<EOF
#!/bin/bash
printf '%s' "\$4" >"$home/sent.txt"
echo "Message sent to \$3"
EOF
  chmod +x "$home/.local/bin/gobby"
  echo "$home"
}

run() { HOME="$1" bash "$1/.gobby/watchdog/watchdog.sh" 2>"$1/stderr.txt"; }

test_hub=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test
baseline="2026-10-01 03:00:00 - INFO     - old.line - baseline"

# --- reachable hub, with an errors alarm so an alert is sent ---
home=$(
  {
    echo "$baseline"
    echo "2026-10-01 03:05:00 - ERROR    - code_index.sync_worker._sync_file - vector sync failed"
    printf 'Traceback (most recent call last):\n  File "/x/sync_worker.py", line 1\n'
    echo "RuntimeError: gcode exited 1"
  } | make_home reachable "$test_hub"
)
check "fixture psql is substituted" grep -qx "psql=$tmp/psql" "$home/.gobby/watchdog/watchdog.sh"
run "$home"
last="$home/.gobby/watchdog/last.txt"
check "gcode process count is sampled" grep -Eq '^gcode processes: [0-9]+$' "$last"
check "hub connection count is sampled" grep -Eq '^hub connections: [1-9][0-9]*$' "$last"
check "samples reach watchdog.log" \
  grep -Eq '^hub connections: [0-9]+$' "$home/.gobby/watchdog/watchdog.log"
check "alert was sent through the fake gobby" test -s "$home/sent.txt"
check "alert leaves out the non-alarm gcode sample" \
  test "$(grep -c '^gcode processes' "$home/sent.txt")" -eq 0
check "alert leaves out the non-alarm connection sample" \
  test "$(grep -c '^hub connections' "$home/sent.txt")" -eq 0
check "state keeps its four-field format" \
  grep -Eq '^[0-9]+ [0-9]+ [a-z,]+ [0-9]+$' "$home/.gobby/watchdog/state"

# --- unreachable hub: no connection sample, the db alarm still fires ---
port=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
home=$(echo "$baseline" | make_home unreachable "postgresql://gobby_test:x@127.0.0.1:$port/gobby_test")
run "$home"
last="$home/.gobby/watchdog/last.txt"
check "db outage keeps its alarm" grep -q '^ALARM\[db\]' "$last"
check "db outage records no connection sample" \
  test "$(grep -c '^hub connections' "$last")" -eq 0
check "gcode sample does not depend on the hub" grep -Eq '^gcode processes: [0-9]+$' "$last"

echo "failures: $fails"
[ "$fails" -eq 0 ]
