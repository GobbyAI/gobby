#!/usr/bin/env bash
set -euo pipefail

log_dir="/var/log/pgaudit"
start=""
end=""

usage() {
  cat <<'EOF'
Usage: pg_audit_export.sh --start <iso8601> --end <iso8601> [--log-dir <path>]

Emit pgAudit AUDIT lines whose PostgreSQL log timestamp falls within the
inclusive validation window.
EOF
}

while (($#)); do
  case "$1" in
    --start)
      if (($# < 2)) || [[ "${2:-}" == --* ]]; then
        echo "--start requires an ISO 8601 timestamp." >&2
        exit 2
      fi
      start="${2:-}"
      shift 2
      ;;
    --end)
      if (($# < 2)) || [[ "${2:-}" == --* ]]; then
        echo "--end requires an ISO 8601 timestamp." >&2
        exit 2
      fi
      end="${2:-}"
      shift 2
      ;;
    --log-dir)
      if (($# < 2)) || [[ "${2:-}" == --* ]]; then
        echo "--log-dir requires a path." >&2
        exit 2
      fi
      log_dir="${2:-}"
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$start" || -z "$end" ]]; then
  echo "Both --start and --end are required." >&2
  usage >&2
  exit 2
fi

if [[ ! -d "$log_dir" ]]; then
  echo "pgAudit log directory not found: $log_dir" >&2
  exit 1
fi

if ! start_epoch="$(date -u -d "$start" +%s)"; then
  echo "Invalid --start timestamp: $start" >&2
  exit 2
fi
if ! end_epoch="$(date -u -d "$end" +%s)"; then
  echo "Invalid --end timestamp: $end" >&2
  exit 2
fi

if ((start_epoch > end_epoch)); then
  echo "--start must be earlier than or equal to --end." >&2
  exit 2
fi

shopt -s nullglob
log_files=("$log_dir"/pgaudit-*.log)
if ((${#log_files[@]} == 0)); then
  exit 0
fi

LC_ALL=C sort -z < <(printf '%s\0' "${log_files[@]}") | while IFS= read -r -d '' log_file; do
  while IFS= read -r line; do
    [[ "$line" == *"AUDIT:"* ]] || continue
    if [[ "$line" =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2})[[:space:]]+([0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?)[[:space:]]+([^[:space:]]+) ]]; then
      line_epoch="$(date -u -d "${BASH_REMATCH[1]} ${BASH_REMATCH[2]} ${BASH_REMATCH[4]}" +%s)"
      if ((line_epoch >= start_epoch && line_epoch <= end_epoch)); then
        printf '%s\n' "$line"
      fi
    fi
  done < "$log_file"
done
