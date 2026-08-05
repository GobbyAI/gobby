# Hub Data Retention Policy

> **Plan ID:** `hub-data-retention`
>
> **Owner epic:** #19379.19654
>
> **Policy task:** #19655

## Context
`kind: framing`

The hub has six high-volume data families with inconsistent lifecycle behavior:
`metrics_events` already rolls raw rows into a lifetime aggregate after 30 days;
`spans` has a 7-day unbounded delete; `token_events`, `loop_progress`, and
`step_executions` have no age policy; and the `recall_*` tables are active
research evidence whose current growth is unbounded. The July 2026 audit found
10.0 million `metrics_events` rows, 984 MB of spans, 352 MB of token events,
240 MB of loop progress, 143 MB of step executions for 32 rows, and about 72 MB
of recall experiment data.

This policy resolves TTL, cadence, batching, indexing, rollout, and monitoring.
It preserves the recall experiment by requiring a verified archive before its
raw online cohorts can be deleted.

## Decision Record
`kind: framing`

1. One hub-global retention loop owns these tables. It runs on startup and then
   every 24 hours. A PostgreSQL advisory lock elects one maintenance owner when
   multiple daemons share the hub; a contender skips the cycle successfully.
2. Default start time is 03:30 UTC. Startup adds a deterministic per-machine
   0-15 minute jitter, while the advisory lock remains the correctness boundary.
3. Direct deletion and payload scrubbing use ordered batches of 10,000 rows,
   at most 20 batches per table per cycle, with a 100 ms cooperative yield
   between batches. Each transaction covers one batch.
4. Recurring maintenance never runs `VACUUM FULL`. PostgreSQL autovacuum reuses
   freed pages; the loop issues `ANALYZE` only after at least 100,000 rows change.
   An operator may schedule a one-time rewrite during a maintenance window.
5. Raw retention uses event time. Active/resumable workflow rows and active
   session progress are exempt until their parent becomes terminal.
6. Recall evidence uses archive-before-delete. A calendar-month cohort is
   written as compressed JSONL plus a SHA-256 manifest and per-table row counts.
   Deletion is fail-closed until a fresh read-back matches the manifest.
7. The authority daemon writes recall archives to the configured shared hub
   backup filesystem. `~/.gobby/backups/retention/recall` is the single-host
   default. All shared-hub daemons must resolve the same path before retention
   is enabled.
8. Fleet ownership is outside this policy. Global maintenance authority comes
   from the advisory lock and shared archive path; machine attribution remains
   owned by #19659.

## Retention Matrix
`kind: framing`

| Data | Online retention | Eligibility and action | Cadence |
|---|---:|---|---:|
| `metrics_events` | 30 days raw | Atomically aggregate the selected IDs into `metrics_events_archive`, then delete exactly those IDs. Archive aggregates remain indefinitely. | Daily, 10,000 rows/batch |
| `token_events` | 180 days | Delete by `event_at`; session-level usage totals remain on `sessions`. Add a global `event_at` index. | Daily, 10,000 rows/batch |
| `loop_progress` | 7 days after parent session is non-active | Clear on terminal session transition; safety sweep joins terminal sessions and deletes through the existing `(session_id, recorded_at)` index. Active sessions are exempt. | Event-driven plus daily, 10,000 rows/batch |
| `step_executions` payloads | 30 days after terminal parent execution | Set `input_json`, `output_json`, and `error` to NULL. Preserve status, timestamps, step ID, and approval audit fields. | Daily, 1,000 rows/batch |
| `step_executions` rows | 365 days after terminal parent execution | Delete the terminal `pipeline_executions` parent so FK cascades remove steps consistently. Active, pending, waiting-approval, and resumable executions are exempt. | Daily, 1,000 parents/batch |
| `spans` | 7 days | Delete ordered by indexed `start_time_ns`; never use nullable `created_at` as the scan boundary. | Daily, 10,000 rows/batch |
| Recall request cohorts | 365 days online | Archive and verify a complete calendar month, then delete child rows and request rows in one bounded campaign. Unarchived rows are never deleted. | Daily check; at most one month/cycle |
| Recall terminal judge state | 90 days | Delete only `complete` or `terminal` rows after their prompt/outcome cohort is in a verified archive. Claimed/retryable rows are exempt. | Daily, 1,000 rows/batch |
| Recall gate/audit ledger | No TTL | Keep `recall_gate_runs`, `recall_holdout_consumed`, and `recall_shadow_audit_verdicts` online indefinitely as experiment and holdout-consumption evidence. | Monthly growth check |

The recall request cohort contains these tables and is archived as one unit:
`recall_signal_requests`, `recall_signal_hits`, `recall_injection_outcomes`,
`recall_usefulness`, `recall_shadow_prompt_snapshot`, and the corresponding
`recall_shadow_judge_state` rows. A cohort month is eligible only when every row
is older than 365 days and no live claim or retry lease references its request
IDs. The archive manifest records schema identity, min/max request time,
per-table counts, uncompressed and compressed byte counts, and SHA-256 hashes.

## Configuration Contract
`kind: framing`

Add one `retention` block under daemon persistence settings:

```yaml
retention:
  enabled: true
  interval_hours: 24
  start_hour_utc: 3
  start_minute_utc: 30
  batch_size: 10000
  max_batches_per_table: 20
  metrics_event_days: 30
  token_event_days: 180
  loop_progress_terminal_days: 7
  step_payload_days: 30
  pipeline_history_days: 365
  span_days: 7
  recall_online_days: 365
  recall_terminal_state_days: 90
  recall_archive_root: ~/.gobby/backups/retention/recall
```

Validation rejects zero/negative intervals and TTLs, a batch size above 50,000,
and an archive root that is absent, not writable, or differs from the path
attested by the current maintenance owner. `enabled=false` disables deletion
while leaving dry-run stats available. Defaults are product policy, not legacy
aliases.

## P0: Schema and Storage Primitives
`kind: framing`

### 0.1 Add retention scan indexes [category: code]
`kind: deliverable`

Targets:
- `crates/gcore/assets/schema/migrations/376_retention_scan_indexes.sql`
- `crates/gcore/src/schema/assets.rs::*` — scope-reason: embed migration 376 and update root identity
- `crates/gcore/assets/schema/catalog.manifest.json::*` — scope-reason: generated index oracle changes atomically

Add `idx_token_events_event_at ON token_events(event_at, id)`. Other policies
reuse existing indexes: `metrics_events(created_at)`,
`loop_progress(session_id, recorded_at DESC)`,
`pipeline_executions(status, updated_at)`, and `spans(start_time_ns)`.

**Acceptance:**
- 0.1.1 - Fresh and adopted hubs reach the same catalog; `EXPLAIN` for each
  retention selector uses its named index; gcore schema identity and catalog
  freshness checks pass. test: `crates/gcore/tests/catalog_manifest_freshness.rs`.

### 0.2 Add bounded storage operations [category: code]
`kind: deliverable`

Targets:
- `src/gobby/storage/retention.py`
- `src/gobby/mcp_proxy/metrics_events.py::MetricsEventStore.archive_old_events`
- `src/gobby/storage/spans.py::SpanStorage.delete_old_spans`
- `src/gobby/autonomous/progress_tracker.py::ProgressTracker.clear_session`
- `src/gobby/storage/pipeline_history.py::PipelineHistoryStorageMixin`

Implement one storage operation per matrix row. Select stable keys in age order,
lock with `FOR UPDATE SKIP LOCKED`, mutate only the selected keys, and return a
typed result containing selected, changed, oldest-remaining timestamp, and
elapsed time. Metrics aggregation and deletion stay in the same transaction.
Recall archive selection uses a repeatable-read snapshot and deletes only after
the archive read-back succeeds.

**Acceptance:**
- 0.2.1 - Focused PostgreSQL tests prove batch bounds, concurrent writers,
  rollback, terminal-parent exemptions, idempotent recall archive retry, and
  archive-verification failure leaving every source row intact. test:
  `tests/storage/test_retention.py`.

## P1: Scheduling and Operations
`kind: framing`

### 1.1 Add one hub-global retention loop [category: code] (depends: 0.1, 0.2)
`kind: deliverable`

Targets:
- `src/gobby/config/retention.py`
- `src/gobby/config/app.py::*` — scope-reason: daemon config owns the new retention block
- `src/gobby/runner_maintenance/retention.py`
- `src/gobby/runner_lifecycle_periodic.py::start_periodic_tasks`

Acquire the advisory lock once per cycle, run the matrix in table order, cap
work per table, and release the lock before sleeping. Cancellation stops after
the current transaction. One table failure records failure telemetry and lets
the next table run; the cycle exits non-green for health reporting.

**Acceptance:**
- 1.1.1 - Isolated two-daemon tests show exactly one owner performs work;
  cancellation is transaction-safe; per-table failures do not suppress later
  tables; configuration bounds and disabled mode are covered. test:
  `tests/runner/test_retention.py`.

### 1.2 Expose retention health [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/servers/routes/admin/_stats.py::*` — scope-reason: admin stats expose retention health and lag
- `src/gobby/telemetry/metrics.py`

Publish cycle duration, selected/changed rows, errors, skipped-lock cycles,
archive bytes, verification failures, and oldest eligible row age by table.
Admin status reports last success and last failure without exposing archive
paths. Alert when a table misses two cycles, eligible lag exceeds twice its
TTL, recall online data exceeds 5 GB or 5 million requests, or archive
verification fails.

**Acceptance:**
- 1.2.1 - Focused route and telemetry tests cover green, overdue, lock-skipped,
  and archive-failure states with no secret/path disclosure. test:
  `tests/servers/routes/admin/test_retention_stats.py`.

## Rollout
`kind: framing`

1. Apply migration 376 with the daemon stopped and a fresh verified hub backup.
2. Run dry mode for 24 hours and record per-table eligible counts and query
   plans. Any sequential scan on a table above 100 MB blocks activation.
3. Enable one batch per table per cycle for seven days. Compare table/index
   sizes, dead tuples, WAL volume, lock waits, and daemon latency before and
   after each cycle.
4. Raise to the default 20-batch cap. Recall deletion remains disabled until a
   synthetic archive in an isolated test database passes manifest read-back and
   restore verification.
5. After the first production recall archive, restore it into an isolated
   database and compare every per-table count and hash before allowing source
   deletion.

Rollback sets `retention.enabled=false`; in-flight work finishes its current
transaction. Direct deletes are recovered from the pre-activation hub backup.
Recall cohorts are recoverable from their verified archives. Aggregate metrics
are idempotent because each batch archives and deletes one exact ID set in one
transaction.

## V1: Verification
`kind: verification`

- Run PostgreSQL tests only with
  `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`
  and `GOBBY_TEST_PROTECT=1`.
- Run Rust integration tests only with
  `GOBBY_TEST_POSTGRES_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test`.
- Use disposable `gobby_test_*` databases for archive/restore and force-drop
  only those validated disposable names.
- Verify the production hub through read-only stats during dry run. Tests and
  destructive rehearsal never use the production database.
- Final activation requires a daemon restart and five continuous minutes with
  no retention-owned warnings or errors.

No policy decisions remain open. Partitioning is excluded until a retained
table exceeds 10 GB after this policy; the bounded indexed loop is the complete
current solution.
