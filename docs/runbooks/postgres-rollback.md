# PostgreSQL Recovery Export Runbook

Use this runbook during the post-cutover validation window when a regression
cannot be fixed forward within 2 hours, or when any data corruption is detected.

Phase 7 does not support restarting Gobby against the legacy PostgreSQL hub runtime.
This runbook captures PostgreSQL-side writes before a fix-forward or
operator-managed PostgreSQL restore path. `gobby postgres uninstall` removes or
disconnects PostgreSQL service artifacts only; it is not a runtime fallback.

Writes made to PostgreSQL during the validation window are at risk during
recovery. The export step below preserves evidence for forensic analysis and
operator-managed repair.

## Before Recovery

1. Stop the daemon:

   ```bash
   gobby stop
   ```

2. Locate the cutover ticket emitted by `gobby postgres activate` and attach the
   path to the recovery ticket:

   ```bash
   ls -1t ~/.gobby/migrations/cutover-*.json | head -n1
   ```

3. Read the ticket fields that select the export path:

   ```bash
   ticket="$HOME/.gobby/migrations/cutover-<timestamp>.json"
   activated_at="$(jq -r '.activated_at' "$ticket")"
   deadline_at="$(jq -r '.deadline_at' "$ticket")"
   capture_kind="$(jq -r '.capture_kind' "$ticket")"
   capture_value="$(jq -r '.capture_value // empty' "$ticket")"
   recovery_end_at="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
   ```

   Use `activated_at` as the export start. Use the current UTC time as the export
   end when recovering before `deadline_at`; set `recovery_end_at` to
   `deadline_at` when exporting a complete validation window after the deadline
   has already been reached.

4. Create a safe artifact directory outside the live database paths:

   ```bash
   artifact_dir="$HOME/.gobby/recovery-artifacts/$(date -u +%Y%m%dT%H%M%SZ)"
   mkdir -p "$artifact_dir"
   cp "$ticket" "$artifact_dir/cutover-ticket.json"
   ```

## Export Validation-Window Writes

Select exactly one export path from `capture_kind`.

### `capture_kind="pgaudit-managed"`

Docker mode stores the pgAudit append-only log inside Gobby's PostgreSQL
container. Export the audit lines for the validation window before recovery:

```bash
docker exec gobby-postgres /usr/local/bin/pg_audit_export.sh \
  --start "$activated_at" \
  --end "$recovery_end_at" \
  > "$artifact_dir/validation-window-pgaudit.log"
```

Supplement the pgAudit export with full snapshots for high-risk tables, or SQL
exports for tables that support `updated_at` filtering:

```bash
pg_dump "$DATABASE_URL" \
  --data-only \
  --table sessions \
  --table tasks \
  > "$artifact_dir/updated-table-snapshot.sql"
```

Use table-specific SQL when a table has an `updated_at` column and a narrower
forensic export is practical:

```bash
psql "$DATABASE_URL" \
  --csv \
  -c "SELECT * FROM tasks WHERE updated_at >= '$activated_at'" \
  > "$artifact_dir/tasks-updated-during-window.csv"
```

### `capture_kind="pgaudit-file"`

Native and external installs with operator-wired pgAudit record the source path
in `capture_value`. Copy the source log and produce a window-filtered artifact:

```bash
cp "$capture_value" "$artifact_dir/source-pgaudit.log"
start_key="$(printf '%s\n' "$activated_at" | sed 's/T/ /; s/Z$//; s/+00:00$//')"
end_key="$(printf '%s\n' "$recovery_end_at" | sed 's/T/ /; s/Z$//; s/+00:00$//')"
awk -v start="$start_key" -v end="$end_key" '
  /AUDIT:/ {
    line_ts = substr($0, 1, 19)
    if (line_ts >= substr(start, 1, 19) && line_ts <= substr(end, 1, 19)) {
      print
    }
  }
' "$capture_value" > "$artifact_dir/validation-window-pgaudit.log"
```

This filter assumes the operator's pgAudit file starts each line with a
PostgreSQL timestamp such as `YYYY-MM-DD HH:MM:SS`. If the operator changed the
log prefix, use their parser to apply the same `activated_at` to
`recovery_end_at` window and attach both the parser command and output.

### `capture_kind="wal-archive"`

Native and external installs with WAL archiving record the archive endpoint or
slot descriptor in `capture_value`. Use the operator's WAL archive runbook to
export the timestamp window from `activated_at` to the recovery end time:

```bash
printf '%s\n' "$capture_value" > "$artifact_dir/wal-archive-source.txt"
```

Attach the exact archive export command output to the rollback ticket. Gobby does
not prescribe the archive-product command because `capture_value` may identify a
managed service, archive bucket, replication slot, or another operator-owned
endpoint.

### `capture_kind="none"`

This branch means activation used `--accept-no-rollback-risk`. There is no
automatic capture sink. Preserve the acknowledgement block and collect
best-effort forensic exports from tables that have `updated_at` columns:

```bash
jq '.acknowledgement' "$ticket" > "$artifact_dir/no-rollback-acknowledgement.json"
psql "$DATABASE_URL" \
  --csv \
  -c "SELECT * FROM tasks WHERE updated_at >= '$activated_at'" \
  > "$artifact_dir/tasks-updated-during-window.csv"
```

The recovery ticket must include the acknowledgement block so the audit chain is
intact.

## Recovery After Export

1. Keep the current bootstrap on PostgreSQL. Do not use `gobby postgres
   uninstall` as a recovery step. Leave `bootstrap.yaml` using a valid
   PostgreSQL `database_url` with mode `0600`.

2. Choose the recovery path:

   - Preferred: fix forward against the PostgreSQL hub, then restart the daemon.
   - If restore is unavoidable: use the exported artifacts with an
     operator-managed PostgreSQL restore path.

3. Start the daemon after the selected recovery path is in place:

   ```bash
   gobby start
   ```

4. Run smoke checks against the recovered environment:

   ```bash
   gobby status
   gobby sessions list
   gobby tasks list
   gobby memory recall "foo"
   gcode search "bar"
   ```

5. Attach the complete artifact directory to the recovery or post-mortem task:

   ```text
   cutover-ticket.json
   validation-window-pgaudit.log, WAL archive export, or no-rollback acknowledgement
   targeted pg_dump / SQL / CSV forensic exports
   recovery path selected after export
   smoke-check results after restart
   ```

6. File a task to repair the blocking regression or corruption cause.

## After the Validation Window

This recovery export path is only for the validation window. A valid
PostgreSQL `database_url` in `bootstrap.yaml` remains required for steady-state
runtime recovery.
