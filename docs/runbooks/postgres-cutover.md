# PostgreSQL Cutover Runbook

This runbook covers PostgreSQL activation gates and validation-window capture
needed before any operator-managed recovery. Once `gobby postgres activate`
succeeds, PostgreSQL is the only supported hub runtime. Writes made during the
validation window are at risk during recovery and must be captured before any
restore path.

## Post-Phase-5 Audit Reference

Cutover is blocked until the integrated post-Phase-5 concurrency re-audit is
green. Use the [`## Post-Phase-5 re-audit`](../postgres-concurrency-audit.md#post-phase-5-re-audit)
section in `docs/postgres-concurrency-audit.md`; the original Phase 4.7 baseline
does not unblock cutover.

Record this reference in the cutover change ticket before activation:

```text
post_phase5_audit_report: docs/postgres-concurrency-audit.md#post-phase-5-re-audit
post_phase5_audit_version: 2
post_phase5_audit_commit: 2aa15634a06e30eb7aff94a24912a8b1b8b5500c
post_phase5_unresolved_high_medium: None
```

If the report changes, update the recorded `post_phase5_audit_commit` here
before cutover. A stale Phase 4.7-only copy, or any audit section without
`audit_version: 2`, is not valid evidence for this gate.

## Pre-Activation

1. Announce the cutover and schedule the validation window.
2. Stop the daemon:

   ```bash
   gobby stop
   ```

3. Install PostgreSQL if this environment has not already been installed:

   ```bash
   gobby postgres install
   ```

4. Verify PostgreSQL health and extensions:

   ```bash
   gobby postgres status --json | jq -e '.healthy == true and .extensions.pg_search == true'
   ```

5. Complete the pre-activation checklist and record it in the cutover change
   ticket:

   - Confirm the post-Phase-5 audit reference above matches the
     `audit_commit` in `docs/postgres-concurrency-audit.md`.
   - Confirm the linked `## Post-Phase-5 re-audit` section reports zero
     unresolved High or Medium findings.
   - Confirm every remediation PR listed by the post-Phase-5 audit is merged.
   - Confirm the MVCC integration tests added or referenced by the re-audit have
     passed for three consecutive CI runs.
   - Confirm the Docker pgAudit write/read probe passes during activation.

## Bootstrap DSN Storage

`gobby postgres install` writes the PostgreSQL DSN directly to
`~/.gobby/bootstrap.yaml`:

```yaml
database_url: postgresql://gobby:gobby_dev@localhost:60891/gobby
```

The installer writes `bootstrap.yaml` with mode `0600`. Startup fails when the
file has broader permissions or when `hub_backend: postgres` is present without
`database_url`.

`gobby postgres status` reports mode, host, database, health, extension
availability, and preload libraries.

`gobby postgres uninstall` is service cleanup. Before restarting the daemon
after uninstalling a PostgreSQL service, preserve or recreate a valid PostgreSQL
`database_url` bootstrap entry. Recreate it by rerunning
`gobby postgres install --mode docker` while the daemon is stopped.

After the validation window closes, the steady-state PostgreSQL runtime still
uses the same `database_url` field. Product-supported recovery must preserve a
valid PostgreSQL DSN.

## Validation Capture

Docker mode uses the pgAudit log managed by the Gobby PostgreSQL image. The
activator probes the extension and log write/read path automatically; no capture
flag is required. Operators may also confirm the log is live before activation:

```bash
docker exec gobby-postgres ls -lh /var/log/pgaudit/
docker exec gobby-postgres sh -c \
  'tail -f "$(find /var/log/pgaudit -name "pgaudit-*.log" -type f | sort | tail -n1)"'
```

Activation fails if Docker pgAudit cannot prove that a write is visible in the
managed audit log. The deprecated `--capture-sink` and
`--accept-no-rollback-risk` flags are not valid for Docker installs.

## Activate

1. Run activation:

   ```bash
   gobby postgres activate
   ```

2. Attach the printed `~/.gobby/migrations/cutover-<timestamp>.json` path to the
   cutover change ticket. The artifact contains `activated_at`, `deadline_at`,
   `capture_kind`, `capture_value`, acknowledgement details when applicable, and
   activation gate results.

3. Start the daemon:

   ```bash
   gobby start
   ```

4. Run smoke checks. Each command must return expected data within expected
   latency:

   ```bash
   gobby status
   gobby sessions list
   gobby tasks list
   gobby memory recall "foo"
   gcode search "bar"
   ```

5. Announce cutover complete and record the validation-window deadline from the
   cutover artifact. The maximum validation window is 48 hours from
   `activated_at`; use `deadline_at` as the recovery decision deadline. If
   blocking regressions remain at the deadline, stop the daemon and use the
   recovery export runbook instead of extending the window silently.

## Validation Watch List

During the validation window, monitor and record:

- MVCC-driven callback regressions identified by the post-Phase-5 audit.
- Search result ordering drift on representative queries.
- Latency regressions greater than 2x baseline on storage-bound endpoints.
- Health of the Docker pgAudit append-only write log.

## Export Audit Window

Before recovery, export PostgreSQL-side writes made during the
validation window. Use `activated_at`, `deadline_at`, `capture_kind`, and
`capture_value` from the cutover artifact.

For Docker-managed pgAudit:

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
artifact and collect best-effort table exports for forensic review.
