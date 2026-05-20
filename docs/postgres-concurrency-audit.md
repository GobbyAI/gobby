---
audit_version: 1
phase_baseline: P4
audit_commit: 4f1607141ea0be23c4c44fd8813b93f83590cb02
audited_at: 2026-05-20T23:31:20Z
---

# PostgreSQL Concurrency Audit

## Phase 4.7 baseline

The original v1 audit artifact was absent from this worktree and from
`git log --all -- docs/postgres-concurrency-audit.md tests/storage/test_postgres_mvcc.py`.
The provenance block above reconstructs the lower-bound commit from the Phase 5
integration history immediately before the importer, validator, and reseeder
landed. The post-Phase-5 re-audit below uses that SHA as `prior_audit_commit`.

| Callback Site | Risk Level | Read-Modify-Write Risk | Isolation Assumption | Constraint Handling | Remediation | Test Coverage |
| --- | --- | --- | --- | --- | --- | --- |
| `TaskManager._notify_listeners` | Low | None | Callback defers in-memory listener notification until the writing transaction commits. | None | No change required. | Covered by existing task lifecycle tests. |
| `SessionStore._notify_session_change` | Low | None | Callback defers in-memory listener notification until the writing transaction commits. | None | No change required. | Covered by existing session lifecycle tests. |
| `PostgresHubDatabase._transaction_context` / `_after_commit` | Low | None | Callbacks run only after PostgreSQL `COMMIT`; cross-session readers need a fresh transaction to avoid stale snapshots. | None | Protocol documentation records the MVCC boundary. | `test_after_commit_async_reader_uses_committed_state`, `test_after_commit_reader_respects_long_running_snapshot` |
| `PostgresTransaction.savepoint()` | Low | None in production callsites | Savepoint rollback affects database writes inside the transaction; post-commit callbacks must read committed state after rollback. | None | No production savepoint callback callsite found. | `test_savepoint_callback_rollback_safe_with_postgres` |

## Post-Phase-5 re-audit

```yaml
audit_version: 2
phase_baseline: P5
audit_commit: 2aa15634a06e30eb7aff94a24912a8b1b8b5500c
prior_audit_commit: 4f1607141ea0be23c4c44fd8813b93f83590cb02
audited_at: 2026-05-20T23:31:20Z
```

Diff command used for the integration delta:

```bash
git log --oneline 4f1607141ea0be23c4c44fd8813b93f83590cb02..2aa15634a06e30eb7aff94a24912a8b1b8b5500c -- src/gobby/storage/ src/gobby/cli/postgres.py src/gobby/storage/migration/
```

Resolved finding: before `2aa15634a06e30eb7aff94a24912a8b1b8b5500c`, the
non-dry-run SQLite-to-Postgres importer performed target readiness checks, row
copy, BM25 index rebuild, sequence reseed, validation, and marker write across
several transactions. Two importer processes could both pass the marker/empty
target checks before either wrote the completion marker. The remediation wraps
the non-dry-run import phase in one PostgreSQL transaction, takes a
transaction-scoped advisory lock, re-runs readiness and marker checks under that
lock, and forces deferred constraints to `IMMEDIATE` before validation/marker
write.

Unresolved High/Medium findings: None.

| Callback Site | Risk Level | Read-Modify-Write Risk | Isolation Assumption | Constraint Handling | Remediation | Test Coverage |
| --- | --- | --- | --- | --- | --- | --- |
| `sqlite_to_postgres.migrate_sqlite_to_postgres` import phase (`src/gobby/storage/migration/sqlite_to_postgres.py:139`) | Low (remediated) | Readiness and marker check are read-then-write gates; serialized by `pg_advisory_xact_lock` inside the import transaction. | Importer-managed concurrent runs serialize on the same transaction-scoped advisory lock; operator still owns excluding unmanaged external writers during cutover. | Deferrable checks are forced before validation and marker write. | `2aa15634a06e30eb7aff94a24912a8b1b8b5500c` wraps import in one transaction and lock. | `test_importer_copy_forces_deferred_constraints_before_return`, `test_deferrable_constraint_is_forced_before_marker` |
| `_run_target_read_only_preflight` and `_probe_external_ownership_sentinel` (`src/gobby/storage/migration/sqlite_to_postgres.py:186`) | Low | None; reads `pg_search`, baseline state, and external ownership sentinel only. | Dry-run and preflight reads are informational and do not publish migration state. | None. | No code change required. | `test_post_phase5_audit_report_frontmatter_and_rows` |
| `_copy_sqlite_rows_to_postgres` (`src/gobby/storage/migration/sqlite_to_postgres.py:343`) | Low | Bulk copy occurs inside the locked import transaction. | Same connection and transaction see copied rows; other sessions cannot observe partial rows before commit. | `SET CONSTRAINTS ALL DEFERRED` allows dependency-ordered copy, then `SET CONSTRAINTS ALL IMMEDIATE` verifies FKs before later phases. | `2aa15634a06e30eb7aff94a24912a8b1b8b5500c` adds the immediate constraint checkpoint. | `test_importer_copy_forces_deferred_constraints_before_return`, `test_deferrable_constraint_is_forced_before_marker` |
| `reseed_identity_sequences` (`src/gobby/storage/migration/reseed.py:89`) | Low (remediated) | `MAX(id)` plus `setval(...)` is read-modify-write sequence state. | Runs after copy inside the locked import transaction, before marker publication. | None. | Covered by the import transaction/lock remediation. | `test_importer_copy_forces_deferred_constraints_before_return`, existing `test_reseed_identity_sequences_uses_max_id_and_empty_table_convention` |
| `validate_migration` sequence, BM25, CHECK, UNIQUE, NOT NULL probes (`src/gobby/storage/migration/validation.py:145`) | Low (remediated) | Validation reads row counts, max IDs, index stats, and constraint samples after copy/reseed. | Runs in the same locked import transaction as copy/reseed/marker, so it validates the exact state that will be committed. | Deferrable constraints have already been forced to `IMMEDIATE`; validation adds explicit catalog/count/sample checks. | Covered by the import transaction/lock remediation. | `test_importer_copy_forces_deferred_constraints_before_return`, `test_deferrable_constraint_is_forced_before_marker` |
| Postgres after-commit callbacks re-evaluated after Phase 5 (`src/gobby/storage/hub/postgres.py:181`, `src/gobby/storage/tasks/_manager.py:190`, `src/gobby/storage/sessions/_bootstrap.py:68`) | Low | None in existing listener callbacks. | Callbacks run after commit; fresh pooled readers see committed state, while long-running readers keep their existing MVCC snapshot. | None. | No code change required. | `test_after_commit_async_reader_uses_committed_state`, `test_after_commit_reader_respects_long_running_snapshot` |
| Backend-neutral concurrent read-modify-write paths using `transaction_immediate` (`src/gobby/storage/hub/postgres.py:161`) | Low | Read-then-update paths use typed PostgreSQL advisory/row locks. | Concurrent writers serialize through `transaction_immediate` lock targets. | None. | No code change required. | `test_read_modify_write_path_serializes_concurrent_writers` |

Unchanged baseline callsites omitted from the table above: SQLite
`LocalDatabase.transaction()` / `_run_after_commit_callbacks()` and the
backend-neutral `Transaction.after_commit()` protocol documentation. Their risk
classification remains Low after the Phase 5 importer integration.
