# Stale Memory Reconciliation After Explicit Restore

**Plan ID:** dream-stale-memory-reconciliation

## Status

`kind: framing`

Implementation-ready design for reconciling PostgreSQL memory rows with their
derived Qdrant and FalkorDB indexes after an operator explicitly restores a
JSONL backup.

## Goal

`kind: framing`

Keep PostgreSQL authoritative while making an explicit memory restore safe and
complete across secondary indexes. The restore is non-destructive: it creates
missing memories and updates a memory only when the backup `updated_at` is newer.
Rows absent from the backup and database versions newer than the backup remain
unchanged.

## Non-goals

`kind: framing`

- No automatic restore during daemon startup, session startup, pull, or cron.
- No exact snapshot replacement or deletion of database-only memories.
- No filesystem/database merge during backup.
- No fuzzy or content-based identity matching during backup or restore.
- No background replay, retention reaper, trust cursor, or sidecar state file.
- No attempt to make JSONL a live replicated datastore.

## Source-of-truth Contract

`kind: framing`

1. PostgreSQL is authoritative for memories and their live/hidden state.
2. `.gobby/memories.jsonl` is a deterministic recovery artifact containing only
   current live scoped memories.
3. `gobby memory backup` publishes a complete replacement backup atomically.
4. `gobby memory restore` is the only filesystem-to-database path.
5. Qdrant and FalkorDB are derived stores. They may be rebuilt from PostgreSQL.

## Restore Admission

`kind: framing`

The restore command resolves the input path, reads the complete file, and
validates every non-empty line before opening a write transaction. Validation
checks:

- every line is a JSON object;
- `_deleted` is rejected;
- IDs and project/session references use valid UUID syntax;
- `content`, `memory_type`, `tags`, `created_at`, and `updated_at` have the
  expected types;
- timestamps are timezone-aware and comparable;
- memory IDs are unique within the file.

Any parse or validation error aborts the restore before the first database
mutation.

## Transactional Database Restore

`kind: framing`

The database phase runs in one PostgreSQL transaction.

For each validated record:

1. Load the current memory by stable ID.
2. Insert when no database row exists.
3. Update when `backup.updated_at > database.updated_at`.
4. Skip when the timestamps are equal or the database version is newer.
5. Preserve any database row whose ID is absent from the backup.
6. Preserve a newer hidden database row rather than reactivating it from an
   older live backup record.
7. Ignore an unknown source session by storing a null source session rather than
   failing the whole restore.

The transaction records the IDs and final content hashes of inserted or updated
rows. This changed set is emitted only after commit; a rollback emits nothing.

## Secondary-index Reconciliation

`kind: framing`

After the PostgreSQL transaction commits, reconcile only the changed IDs.

### Qdrant

`kind: framing`

For each changed live memory:

1. Generate an embedding from the committed PostgreSQL content.
2. Upsert the vector using the memory UUID as the point identity.
3. Store the committed content hash and project ID in the payload.

If Qdrant is unavailable, leave PostgreSQL committed and enqueue the memory ID
in the existing durable vector reindex state. The normal retry path reads the
current PostgreSQL row before writing, so a delayed job cannot reintroduce stale
backup content.

### FalkorDB

`kind: framing`

For each changed live memory:

1. Remove graph entities and relationships owned only by the prior indexed
   version of that memory.
2. Extract entities from the committed PostgreSQL content.
3. Upsert the memory-to-entity and entity-to-entity relationships.
4. Mark graph processing complete with the committed content hash.

If graph extraction fails, persist the existing graph retry state and let the
normal retry worker re-read PostgreSQL. A graph failure does not roll back the
authoritative memory restore.

### Cross-references

`kind: framing`

Rebuild cross-reference edges for the changed memories and any directly
affected neighbors after vector and graph submission. The rebuild always reads
current live PostgreSQL rows and excludes hidden memories.

Cross-reference reconciliation has its own PostgreSQL-backed retry state keyed
by memory ID and committed content hash. The restore transaction records changed
IDs in that state before commit; workers claim rows with bounded leases, rebuild
from current PostgreSQL state, and mark a row complete only after all affected
edges are durable. Failures retain the row with attempt count, next-attempt time,
and last error. Daemon startup resumes due rows, and an expired lease is safely
reclaimed, so a crash after the memory commit or during edge rebuilding cannot
lose the reconciliation obligation. Re-enqueuing the same ID and content hash is
idempotent.

## Failure Semantics

`kind: framing`

- File validation failure: no PostgreSQL or derived-store mutation.
- PostgreSQL transaction failure: rollback all restored rows; no reconciliation
  work is emitted.
- Qdrant, FalkorDB, or cross-reference failure after commit: report a partial
  reconciliation result, retain the corresponding durable retry row, and keep
  PostgreSQL committed.
- Process crash after PostgreSQL commit: the durable derived-store retry state
  makes reconciliation resumable from PostgreSQL.
- Re-running the same restore: database writes are timestamp no-ops and do not
  enqueue duplicate reconciliation work.

## Operator Result

`kind: framing`

`gobby memory restore` and MCP `restore_memories` return:

- validated record count;
- inserted count;
- updated count;
- skipped count;
- queued vector reconciliation count;
- queued graph reconciliation count;
- cross-reference reconciliation count;
- per-secondary-store errors, if any.

Quiet CLI mode suppresses normal output but preserves a non-zero exit status for
file validation or database failures. Derived-store degradation is reported as a
successful database restore with pending reconciliation.

## P1: Restore Reconciliation

`kind: framing`

**Goal:** Explicit restore updates PostgreSQL transactionally and durably
reconciles every derived memory projection from committed state.

### 1.1 Implement explicit restore and restart-safe derived reconciliation [category: code]

`kind: deliverable`

Targets: `src/gobby/sync/memories.py`, `src/gobby/cli/memory/export.py`,
`src/gobby/mcp_proxy/tools/memory.py`, `tests/sync/test_memory_backup.py`,
`tests/cli/test_memory_cli.py`

1. Keep complete-file validation in `MemoryBackupManager.restore` ahead of the
   transaction.
2. Extend the restore result to retain changed IDs and committed hashes without
   changing last-write-wins semantics.
3. Submit changed IDs to the existing vector reindex state after commit.
4. Submit changed IDs to the existing memory graph retry state after commit.
5. Add PostgreSQL-backed cross-reference retry rows with idempotent enqueue,
   bounded leases, retry scheduling, and restart reclamation; rebuild changed
   IDs and affected live neighbors from current PostgreSQL rows.
6. Expose the reconciliation counts in CLI and MCP restore results.
7. Ensure daemon and session initialization only construct services and never
   invoke restore.

**Acceptance:**

- 1.1.1 - Restore validates `memory_type` and the complete input before its first
  database mutation, then applies last-write-wins updates in one transaction.
  test: `tests/sync/test_memory_backup.py::test_restore_validates_before_transaction`.
- 1.1.2 - Committed changed IDs enqueue vector, graph, and cross-reference work
  from current PostgreSQL state; a rollback enqueues none. test:
  `tests/sync/test_memory_backup.py::test_restore_enqueues_only_after_commit`.
- 1.1.3 - Cross-reference retry state is PostgreSQL-backed, idempotent by memory
  ID and content hash, lease-bounded, and reclaimed after process restart. test:
  `tests/sync/test_memory_backup.py::test_cross_reference_retry_survives_restart`.
- 1.1.4 - CLI and MCP results report committed restore and pending projection
  counts without treating derived-store degradation as database rollback. file:
  `src/gobby/cli/memory/export.py`.
- 1.1.5 - Daemon and session startup never invoke filesystem-to-database
  restore. test: `tests/test_runner_init.py::test_memory_backup_manager_no_automatic_restore`.

## Verification

`kind: framing`

Focused tests must prove:

- malformed input aborts before mutation;
- a `_deleted` record aborts before mutation;
- restore creates missing memories;
- restore updates only when the backup timestamp wins;
- restore preserves absent, newer, and newer-hidden database rows;
- a database rollback emits no derived-store work;
- committed changed IDs enqueue Qdrant and FalkorDB reconciliation;
- retry workers re-read PostgreSQL rather than retaining backup payloads;
- cross-reference rebuilding excludes hidden memories;
- a crash after restore commit or during cross-reference rebuilding leaves a
  durable due or lease-expired row that a restarted daemon processes at least
  once; derived effects are idempotent when keyed by the committed content hash;
- re-running an identical restore is a no-op;
- daemon and session startup never invoke restore.

## Acceptance Criteria

`kind: framing`

- PostgreSQL remains the only authoritative memory store.
- JSONL backup is live-row-only, deterministic, and atomically replaced.
- Restore is explicit, fully validated, transactional, non-destructive, and
  last-write-wins by stable ID.
- Secondary indexes converge from committed PostgreSQL state through durable,
  restart-safe retry mechanisms, including cross-reference reconciliation.
- No automatic filesystem-to-database path exists.

## M1 Task Manifest

`kind: manifest`

```yaml
- title: Implement explicit memory restore reconciliation
  category: code
  task_type: feature
  depends_on: []
  validation_criteria: Explicit restore is transactional and all derived projections converge through durable restart-safe retry state.
  labels:
  - covers:dream-stale-memory-reconciliation:1.1:1.1.1
  - covers:dream-stale-memory-reconciliation:1.1:1.1.2
  - covers:dream-stale-memory-reconciliation:1.1:1.1.3
  - covers:dream-stale-memory-reconciliation:1.1:1.1.4
  - covers:dream-stale-memory-reconciliation:1.1:1.1.5
  implementation_domain: backend
  tdd: true
  source_section: '1.1'
```
