#!/usr/bin/env bash
set -euo pipefail

readonly DEFAULT_LOG_DIR="/var/log/pgaudit"

log_dir="$DEFAULT_LOG_DIR"
start=""
end=""

usage() {
  cat <<'EOF'
Usage: pg_audit_export.sh --start <iso8601> --end <iso8601> [--log-dir <path>]

Emit pgAudit AUDIT lines whose PostgreSQL log timestamp falls within the
inclusive validation window.
EOF
}

die_usage() {
  echo "$1" >&2
  usage >&2
  exit 2
}

require_value() {
  local flag="$1"
  local value="${2:-}"
  local description="$3"

  if [[ -z "$value" || "$value" == --* ]]; then
    echo "$flag requires $description." >&2
    exit 2
  fi

  printf '%s\n' "$value"
}

parse_epoch() {
  local flag="$1"
  local timestamp="$2"
  local epoch

  if ! epoch="$(timestamp_epoch "$timestamp")"; then
    echo "Invalid $flag timestamp: $timestamp" >&2
    exit 2
  fi

  printf '%s\n' "$epoch"
}

timestamp_epoch() {
  local timestamp="$1"
  local portable_timestamp

  if date -u -d "$timestamp" +%s 2>/dev/null; then
    return
  fi

  portable_timestamp="$timestamp"
  if [[ "$portable_timestamp" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T ]]; then
    portable_timestamp="${portable_timestamp/T/ }"
  fi
  if [[ "$portable_timestamp" == *Z ]]; then
    portable_timestamp="${portable_timestamp%Z} UTC"
  fi
  if [[ "$portable_timestamp" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})[[:space:]]+([0-9]{2}:[0-9]{2}:[0-9]{2})(\.[0-9]+)?[[:space:]]+([^[:space:]]+)$ ]]; then
    date -u -j -f "%Y-%m-%d %H:%M:%S %Z" \
      "${BASH_REMATCH[1]} ${BASH_REMATCH[2]} ${BASH_REMATCH[4]}" +%s 2>/dev/null
    return
  fi

  return 1
}

audit_line_epoch() {
  local line="$1"

  if [[ "$line" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})[[:space:]]+([0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?)[[:space:]]+([^[:space:]]+) ]]; then
    timestamp_epoch "${BASH_REMATCH[1]} ${BASH_REMATCH[2]} ${BASH_REMATCH[4]}"
    return
  fi

  return 1
}

emit_windowed_audit_lines() {
  local start_epoch="$1"
  local end_epoch="$2"
  local -a log_files=("${@:3}")
  local line
  local line_epoch
  local log_file

  LC_ALL=C sort -z < <(printf '%s\0' "${log_files[@]}") | while IFS= read -r -d '' log_file; do
    while IFS= read -r line; do
      [[ "$line" == *"AUDIT:"* ]] || continue
      if line_epoch="$(audit_line_epoch "$line")" \
        && ((line_epoch >= start_epoch && line_epoch <= end_epoch)); then
        printf '%s\n' "$line"
      fi
    done < "$log_file"
  done
}

while (($#)); do
  case "$1" in
    --start)
      start="$(require_value "--start" "${2:-}" "an ISO 8601 timestamp")"
      shift 2
      ;;
    --end)
      end="$(require_value "--end" "${2:-}" "an ISO 8601 timestamp")"
      shift 2
      ;;
    --log-dir)
      log_dir="$(require_value "--log-dir" "${2:-}" "a path")"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die_usage "Unknown argument: $1"
      ;;
  esac
done

if [[ -z "$start" || -z "$end" ]]; then
  die_usage "Both --start and --end are required."
fi

if [[ ! -d "$log_dir" ]]; then
  echo "pgAudit log directory not found: $log_dir" >&2
  exit 1
fi

start_epoch="$(parse_epoch "--start" "$start")"
end_epoch="$(parse_epoch "--end" "$end")"

if ((start_epoch > end_epoch)); then
  echo "--start must be earlier than or equal to --end." >&2
  exit 2
fi

shopt -s nullglob
log_files=("$log_dir"/pgaudit-*.log)
if ((${#log_files[@]} == 0)); then
  exit 0
fi

emit_windowed_audit_lines "$start_epoch" "$end_epoch" "${log_files[@]}"
