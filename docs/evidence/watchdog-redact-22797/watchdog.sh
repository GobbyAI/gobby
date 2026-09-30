#!/bin/bash
# Gobby load watchdog (Josh's go, 2026-09-22 10:1x). Replaces the LLM janitor loop
# until a gdaemon heartbeat registrant (#22411, epic #21542) supersedes it.
#
# Runs every 5 min from ~/Library/LaunchAgents/com.gobby.watchdog.plist under macOS
# /bin/bash 3.2. Read-only: SQL runs in a read-only transaction. Every run writes
# last.txt and appends to watchdog.log. It alerts only on a runbook-threshold breach:
# Telegram via `gobby comms send`, falling back to a macOS notification when the
# daemon can't take it. The same alarm set is not re-sent within 30 minutes.
#
# Remove: launchctl bootout "gui/$(id -u)/com.gobby.watchdog"
#         rm ~/Library/LaunchAgents/com.gobby.watchdog.plist; rm -r ~/.gobby/watchdog
set -u

dir="$HOME/.gobby/watchdog"
state="$dir/state"
err="$HOME/.gobby/logs/errors.log"
dlog="$HOME/.gobby/logs/daemon.log"
psql=/opt/homebrew/opt/libpq/bin/psql
gobby="$HOME/.local/bin/gobby"
cooldown=1800

# Connection parts go to libpq through the environment, so the password never
# appears in a process listing.
db_env() {
  local url re
  url=$(awk '/^database_url:/ {print $2; exit}' "$HOME/.gobby/bootstrap.yaml")
  url=${url//\"/}
  re='^postgres(ql)?://([^:@/]+)(:([^@/]*))?@([^:/]+)(:([0-9]+))?/([^?]+)'
  [[ $url =~ $re ]] || return 1
  export PGUSER="${BASH_REMATCH[2]}" PGPASSWORD="${BASH_REMATCH[4]}"
  export PGHOST="${BASH_REMATCH[5]}" PGPORT="${BASH_REMATCH[7]:-5432}"
  export PGDATABASE="${BASH_REMATCH[8]}"
  export PGOPTIONS='-c default_transaction_read_only=on' PGCONNECT_TIMEOUT=5
}

q() {
  "$psql" -X -At -F ' | ' -c "$1" 2>/dev/null
}

# Run a command with a wall-clock limit (macOS ships no timeout(1)).
with_timeout() {
  local secs=$1
  shift
  perl -e 'alarm shift; exec @ARGV or die "exec $ARGV[0]: $!\n"' "$secs" "$@"
}

# Summarize new errors.log lines (stdin) into phone-readable alarm evidence.
# $1 is the physical new-line count. An event is one log entry (header plus its
# traceback) that has a traceback, minus known-benign ones, or that mentions a
# known-bad string. Events group by signature (logger.function and message with
# UUIDs and host paths collapsed); the three most frequent print with count and time range. The
# log lines themselves travel as a redacted document (attach_errors), never in the
# alert text. Prints nothing when there are no events.
error_evidence() {
  awk -v lines="$1" '
    BEGIN {
      h = "[0-9a-f]"; h4 = h h h h
      uuid = h4 h4 "-" h4 "-" h4 "-" h4 "-" h4 h4 h4
      # An absolute or ~/ path after a space, quote, bracket, =, : or , (so cwd:/x and
      # file:///x are caught); \047 is a single quote.
      path = "[ \t\047\"([{<=:,]~?/[^ \t\047\",;)\\]}>]*"
      bad = "pool acquisition failed|DatabaseExecutor is shut down|HostEpochChangedError|spawn_rollback"
      benign = "search_tool_result - Failed to search stored tool result"
    }
    function flush(   t, sig, key) {
      if (hdr == "" && orphan == "") return
      if (hdr != "" && !((tb && hdr !~ benign) || hit)) { hdr = ""; tb = hit = 0; return }
      if (hdr != "") {
        t = substr(hdr, 12, 8)
        sig = substr(hdr, 21)
        sub(/^- [A-Z]+ *- /, "", sig)
      } else {
        t = "?"; sig = orphan
      }
      while (match(sig, uuid)) sig = substr(sig, 1, RSTART - 1) "<id>" substr(sig, RSTART + RLENGTH)
      # A host path is unreadable on a phone and may name private files: keep the
      # delimiter before it, replace the path itself.
      sig = " " sig
      while (match(sig, path)) sig = substr(sig, 1, RSTART) "<path>" substr(sig, RSTART + RLENGTH)
      sig = substr(sig, 2)
      key = substr(sig, 1, 200)
      if (!(key in count)) { order[++nsig] = key; first[key] = t }
      count[key]++; last[key] = t; events++
      hdr = ""; orphan = ""; tb = hit = 0
    }
    /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] / {
      flush(); hdr = $0; if ($0 ~ bad) hit = 1; next
    }
    /^Traceback \(most recent call last\)/ { tb = 1; next }
    $0 ~ bad { hit = 1; if (hdr == "" && orphan == "") orphan = $0 }
    END {
      flush()
      if (!events) exit
      printf "ALARM[errors]: %d alarm event%s, %d signature%s, in +%d new errors.log lines\n",
        events, (events == 1 ? "" : "s"), nsig, (nsig == 1 ? "" : "s"), lines
      for (r = 1; r <= 3 && r <= nsig; r++) {
        best = 0
        for (j = 1; j <= nsig; j++)
          if (!(j in done) && (best == 0 || count[order[j]] > count[order[best]])) best = j
        done[best] = 1; k = order[best]
        span = (first[k] == last[k]) ? first[k] : first[k] "-" last[k]
        printf "• %dx %s %s\n", count[k], span, k
      }
      if (nsig > 3) printf "(+%d more signature%s)\n", nsig - 3, (nsig - 3 == 1 ? "" : "s")
    }'
}

notify() {
  local msg=$1 short=$2
  if (cd "$HOME/Projects/gobby" && with_timeout 30 "$gobby" comms send --redact gobby-telegram "$msg") \
    >>"$dir/watchdog.log" 2>&1; then
    return 0
  fi
  osascript -e 'on run argv' \
    -e 'display notification (item 1 of argv) with title "Gobby watchdog ALARM"' \
    -e 'end run' "$short" >/dev/null 2>&1
  return 1
}

# Send errors.log lines $2..$3 as a redacted document after a Telegram alarm. The
# CLI scrubs secrets and home paths and, over its 64 KiB cap, sends a one-line
# omission note instead. Only content leaves the machine; the daemon reads no path.
attach_errors() {
  (cd "$HOME/Projects/gobby" && sed -n "$1,$2p" "$err" |
    with_timeout 30 "$gobby" comms attach --caption "errors.log new lines, redacted" \
      gobby-telegram errors-new.txt) >>"$dir/watchdog.log" 2>&1
}

check() {
  local load l1 runs builds cur_err base new evidence cur_vec vbase dvec
  date '+%Y-%m-%d %H:%M:%S'
  load=$(sysctl -n vm.loadavg | tr -d '{}' | awk '{print $1" "$2" "$3}')
  echo "load: $load"
  l1=${load%% *}
  if awk -v l="$l1" 'BEGIN{exit !(l>24)}'; then echo "ALARM[load]: 1-min load $l1 > 24"; fi

  runs=""
  if db_env; then runs=$(q "select count(*) from agent_runs where status='running'"); fi
  if [ -z "$runs" ]; then
    echo "ALARM[db]: database query failed (hub unreachable?)"
  else
    echo "running runs: $runs"
    if [ "$runs" -ge 8 ]; then echo "ALARM[runs]: running runs $runs >= 8"; fi
    q "select left(id::text,8)||' '||coalesce(agent_name,'?') from agent_runs where status='running'" |
      sed 's/^/  /'
  fi

  builds=$(pgrep -x cargo | wc -l | tr -d ' ')
  echo "cargo processes: $builds"
  if [ "$builds" -ge 3 ]; then echo "ALARM[cargo]: $builds concurrent cargo builds"; fi

  cur_err=$(wc -l <"$err" | tr -d ' ')
  base=$prev_err
  if [ "$prev_err" -eq 0 ] || [ "$cur_err" -lt "$prev_err" ]; then base=$cur_err; fi
  new=$((cur_err - base))
  echo "errors.log: $cur_err lines (+$new)"
  if [ "$new" -gt 0 ]; then
    evidence=$(tail -n "$new" "$err" | error_evidence "$new")
    if [ -n "$evidence" ]; then printf '%s\n' "$evidence"; fi
  fi

  cur_vec=$(grep -c "transient vector sync failure" "$dlog" 2>/dev/null)
  cur_vec=${cur_vec:-0}
  vbase=$prev_vec
  if [ "$prev_vec" -eq 0 ] || [ "$cur_vec" -lt "$prev_vec" ]; then vbase=$cur_vec; fi
  dvec=$((cur_vec - vbase))
  echo "vector-sync failures: $cur_vec (+$dvec)"
  if [ "$dvec" -gt 20 ]; then echo "ALARM[vector]: vector-sync failures +$dvec > 20"; fi

  echo "#counters $cur_err $cur_vec"
}

main() {
  local out counters sig now msg short cur new
  prev_err=0 prev_vec=0 last_sig=none last_sent=0
  if [ -f "$state" ]; then read -r prev_err prev_vec last_sig last_sent <"$state"; fi

  out=$(check)
  counters=$(printf '%s\n' "$out" | awk '/^#counters/ {print $2" "$3}')
  out=$(printf '%s\n' "$out" | grep -v '^#counters')
  printf '%s\n' "$out" >"$dir/last.txt"
  printf '%s\n' "$out" >>"$dir/watchdog.log"
  tail -n 2000 "$dir/watchdog.log" >"$dir/watchdog.log.tmp" && mv "$dir/watchdog.log.tmp" "$dir/watchdog.log"

  sig=$(printf '%s\n' "$out" | sed -n -E 's/^ALARM\[([a-z]+)\].*/\1/p' | sort -u | paste -sd, -)
  sig=${sig:-none}
  now=$(date +%s)
  if [ "$sig" != none ] && { [ "$sig" != "$last_sig" ] || [ $((now - last_sent)) -ge "$cooldown" ]; }; then
    # Self-contained for a phone: every line except the per-run listing and the
    # non-alarm cargo/vector counters; the ALARM lines carry those when they breach.
    msg="Gobby watchdog ALARM ($(hostname -s)):
$(printf '%s\n' "$out" | grep -v -E '^(  [0-9a-f]{8} |cargo|vector)')"
    short=$(printf '%s\n' "$out" | grep '^ALARM' | head -3 | paste -sd' ' -)
    if notify "$msg" "$short"; then
      case ",$sig," in
        *,errors,*)
          # The exact window check() summarized: lines prev+1..cur of errors.log.
          cur=${counters%% *}
          new=$(printf '%s\n' "$out" | sed -n -E 's/^errors\.log: [0-9]+ lines \(\+([0-9]+)\)$/\1/p')
          attach_errors $((cur - new + 1)) "$cur"
          ;;
      esac
    fi
    last_sent=$now
  fi
  printf '%s %s %s\n' "${counters:-0 0}" "$sig" "$last_sent" >"$state"
}

main "$@"
