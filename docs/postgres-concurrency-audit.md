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
git log --oneline 4f1607141ea0be23c4c44fd8813b93f83590cb02..2aa15634a06e30eb7aff94a24912a8b1b8b5500c -- src/gobby/storage/ src/gobby/cli/postgres.py
```

Resolved finding: the SQLite-to-Postgres importer covered by this audit has
since been removed. The remaining active scope is PostgreSQL runtime
concurrency.

Unresolved High/Medium findings: None.

| Callback Site | Risk Level | Read-Modify-Write Risk | Isolation Assumption | Constraint Handling | Remediation | Test Coverage |
| --- | --- | --- | --- | --- | --- | --- |
| Postgres after-commit callbacks re-evaluated after Phase 5 (`src/gobby/storage/hub/postgres.py:181`, `src/gobby/storage/tasks/_manager.py:190`, `src/gobby/storage/sessions/_bootstrap.py:68`) | Low | None in existing listener callbacks. | Callbacks run after commit; fresh pooled readers see committed state, while long-running readers keep their existing MVCC snapshot. | None. | No code change required. | `test_after_commit_async_reader_uses_committed_state`, `test_after_commit_reader_respects_long_running_snapshot` |
| Backend-neutral concurrent read-modify-write paths using `transaction_immediate` (`src/gobby/storage/hub/postgres.py:161`) | Low | Read-then-update paths use typed PostgreSQL advisory/row locks. | Concurrent writers serialize through `transaction_immediate` lock targets. | None. | No code change required. | `test_read_modify_write_path_serializes_concurrent_writers` |

Unchanged baseline callsites omitted from the table above: SQLite
`LocalDatabase.transaction()` / `_run_after_commit_callbacks()` and the
backend-neutral `Transaction.after_commit()` protocol documentation. Their risk
classification remains Low after the Phase 5 importer integration.
