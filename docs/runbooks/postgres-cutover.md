# PostgreSQL Cutover Runbook

This runbook covers the cold cutover from SQLite to PostgreSQL and the
validation-window audit capture used for rollback forensics.

## Pre-Activation

1. Announce the cutover window.
2. Stop the daemon:

   ```bash
   gobby stop
   ```

3. Back up the SQLite hub database and record its digest:

   ```bash
   cp ~/.gobby/gobby-hub.db ~/.gobby/gobby-hub.db.$(date -u +%Y%m%dT%H%M%SZ).bak
   sha256sum ~/.gobby/gobby-hub.db ~/.gobby/gobby-hub.db.*.bak
   ```

4. Install PostgreSQL if needed, then import SQLite:

   ```bash
   gobby postgres install
   gobby postgres migrate-from-sqlite --source ~/.gobby/gobby-hub.db --target "$DATABASE_URL"
   ```

5. Confirm migration completion:

   ```bash
   gobby postgres status --json | jq -e '.migration_complete.present == true'
   ```

6. For external mode, confirm the ownership sentinel:

   ```bash
   gobby postgres status --json | jq -e '.ownership.sentinel_present == true'
   ```

## Validation Capture

Docker mode uses the pgAudit log managed by the Gobby PostgreSQL image. Confirm
that log files exist and are growing:

```bash
docker exec gobby-postgres ls -lh /var/log/pgaudit/
```

Confirm live capture before activation by tailing the newest log:

```bash
docker exec gobby-postgres sh -c 'tail -f "$(find /var/log/pgaudit -name "pgaudit-*.log" -type f | sort | tail -n1)"'
```

Native and external modes require one explicit activation choice:

```bash
gobby postgres activate --capture-sink pgaudit-file:/absolute/path/to/pgaudit.log
gobby postgres activate --capture-sink wal-archive:slot-or-archive-dsn
gobby postgres activate --accept-no-rollback-risk
```

The no-rollback-risk path requires typing `I accept no-rollback risk` at the
prompt. Docker mode rejects these flags because pgAudit is the managed gate.

## Activate

1. Run activation:

   ```bash
   gobby postgres activate
   ```

2. Save the printed `~/.gobby/migrations/cutover-<timestamp>.json` path in the
   cutover ticket. The JSON file contains `activated_at`, `deadline_at`,
   `capture_kind`, `capture_value`, and verification detail.
3. Start the daemon:

   ```bash
   gobby start
   ```

4. Run smoke checks:

   ```bash
   gobby status
   gobby sessions list
   gobby tasks list
   gobby memory search "foo"
   gobby code search "bar"
   ```

The validation window starts when activation succeeds. Treat the `deadline_at`
field in the cutover ticket as the 48-hour rollback deadline.

## Export Audit Window

Before rollback or deactivation, export Docker-managed pgAudit lines using the
window from the cutover ticket:

```bash
docker exec gobby-postgres /usr/local/bin/pg_audit_export.sh \
  --start <activated_at> \
  --end <deadline_at> \
  > validation-window-pgaudit.log
```

For `capture_kind="pgaudit-file"`, filter the operator-provided file at
`capture_value` over the same window. For `capture_kind="wal-archive"`, use the
operator's WAL archive tooling for the same timestamps. For
`capture_kind="none"`, preserve the acknowledgement block from the cutover
ticket and collect best-effort table exports for forensic review.
