# Stale Memory Reconciliation After Explicit Restore

## Status

Implementation-ready design for reconciling PostgreSQL memory rows with their
derived Qdrant and FalkorDB indexes after an operator explicitly restores a
JSONL backup.

## Goal

Keep PostgreSQL authoritative while making an explicit memory restore safe and
complete across secondary indexes. The restore is non-destructive: it creates
missing memories and updates a memory only when the backup `updated_at` is newer.
Rows absent from the backup and database versions newer than the backup remain
unchanged.

## Non-goals

- No automatic restore during daemon startup, session startup, pull, or cron.
- No exact snapshot replacement or deletion of database-only memories.
- No filesystem/database merge during backup.
- No fuzzy or content-based identity matching during backup or restore.
- No background replay, retention reaper, trust cursor, or sidecar state file.
- No attempt to make JSONL a live replicated datastore.

## Source-of-truth Contract

1. PostgreSQL is authoritative for memories and their live/hidden state.
2. `.gobby/memories.jsonl` is a deterministic recovery artifact containing only
   current live scoped memories.
3. `gobby memory backup` publishes a complete replacement backup atomically.
4. `gobby memory restore` is the only filesystem-to-database path.
5. Qdrant and FalkorDB are derived stores. They may be rebuilt from PostgreSQL.

## Restore Admission

The restore command resolves the input path, reads the complete file, and
validates every non-empty line before opening a write transaction. Validation
checks:

- every line is a JSON object;
- `_deleted` is rejected;
- IDs and project/session references use valid UUID syntax;
- `content`, `type`, `tags`, `created_at`, and `updated_at` have the
  expected types;
- timestamps are timezone-aware and comparable;
- memory IDs are unique within the file.

Any parse or validation error aborts the restore before the first database
mutation.

## Transactional Database Restore

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

After the PostgreSQL transaction commits, reconcile only the changed IDs.

### Qdrant

For each changed live memory:

1. Generate an embedding from the committed PostgreSQL content.
2. Upsert the vector using the memory UUID as the point identity.
3. Store the committed content hash and project ID in the payload.

If Qdrant is unavailable, leave PostgreSQL committed and enqueue the memory ID
in the existing durable vector reindex state. The normal retry path reads the
current PostgreSQL row before writing, so a delayed job cannot reintroduce stale
backup content.

### FalkorDB

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

Rebuild cross-reference edges for the changed memories and any directly
affected neighbors after vector and graph submission. The rebuild always reads
current live PostgreSQL rows and excludes hidden memories.

## Failure Semantics

- File validation failure: no PostgreSQL or derived-store mutation.
- PostgreSQL transaction failure: rollback all restored rows; no reconciliation
  work is emitted.
- Qdrant, FalkorDB, or cross-reference failure after commit: report a partial
  reconciliation result, persist retry state, and keep PostgreSQL committed.
- Process crash after PostgreSQL commit: the durable derived-store retry state
  makes reconciliation resumable from PostgreSQL.
- Re-running the same restore: database writes are timestamp no-ops and do not
  enqueue duplicate reconciliation work.

## Operator Result

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

## Implementation Work

1. Keep complete-file validation in `MemoryBackupManager.restore` ahead of the
   transaction.
2. Extend the restore result to retain changed IDs and committed hashes without
   changing last-write-wins semantics.
3. Submit changed IDs to the existing vector reindex state after commit.
4. Submit changed IDs to the existing memory graph retry state after commit.
5. Rebuild cross-references for changed IDs and affected live neighbors.
6. Expose the reconciliation counts in CLI and MCP restore results.
7. Ensure daemon and session initialization only construct services and never
   invoke restore.

## Verification

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
- re-running an identical restore is a no-op;
- daemon and session startup never invoke restore.

## Acceptance Criteria

- PostgreSQL remains the only authoritative memory store.
- JSONL backup is live-row-only, deterministic, and atomically replaced.
- Restore is explicit, fully validated, transactional, non-destructive, and
  last-write-wins by stable ID.
- Secondary indexes converge from committed PostgreSQL state through existing
  durable retry mechanisms.
- No automatic filesystem-to-database path exists.
