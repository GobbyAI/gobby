# Code-Index Vector Reconciliation and Path-Aware Content Retention

Plan artifact: `.gobby/plans/code-index-vector-reconciliation.md`

**Plan ID:** code-index-vector-reconciliation

## Overview
`kind: framing`

Task #22958 (Plan code-index symbol-vector reconciliation and path-aware
content retention) under the Lane 7 planning epic. Source evidence: the
read-only archive report
`~/.gobby/research-archive/code-index-storage-gaps-2026-09-26.md`. This plan
cites it and never edits it. Its counts are historical. The source trace was
taken at `cf88559ef2`.

The plan does three things:
- It stops new drift. gcode becomes the only owner of vector-sync completion,
  through a compare-and-set that mirrors the graph one (1.1, 1.2). A re-parse
  of an existing content version marks its vectors pending when the stored
  symbols change (1.3).
- `gcode vector reconcile` replaces `gcode vector cleanup-orphans`. A
  read-only dry-run inventories stale points and missing vectors for one
  project collection (2.1). `--apply`, run under the maintenance lease,
  deletes only point IDs that PostgreSQL no longer has, resets the sync flag
  of synced versions whose points are missing, and writes a receipt (2.2).
- Content GC protects a version only when its exact path and content hash
  appear in recent git history, and only when that path is still eligible
  under the project's current indexing config (3.1, 3.2).

Ownership:
- The `gobby-code` crate implements 1.1, 1.3, P2 and P3.
- The Python daemon implements 1.2.
- 4.1 is documentation.
- The Orchestrator routes the leaves to developer seats.
- The Live Maintenance Procedure is run by an operator the Orchestrator
  names, after Josh approves the dry-run digest. This plan ships no live
  deletion.

Out of scope:
- Live vector deletion.
- PostgreSQL purges.
- GC runs.
- #22959 (Investigate current-daemon cleanup gaps across project removal,
  worktrees, caches and telemetry).
- #22956 (Decide retention and cleanup for token_events,
  unmodeled_observation_events, dropped-column TOAST and the old cargo target).

## Decision Record
`kind: framing`

1. **Topology.** The #19743 (Implement machine-aware code index storage
   (hybrid content-addressed model)) Decision Record gives every daemon one
   shared hub stack: PostgreSQL, Qdrant and FalkorDB. Projections are
   hub-hosted. Reconcile therefore works hub-wide on one project collection,
   under the hub-wide advisory lease. No step depends on which machine runs it.
2. **Deletion authority.** Reconcile deletes a point only when its ID is
   absent from `code_symbols` for that project. The check is repeated under
   the maintenance lease. File path, payload shape and this machine's file
   states never authorize a deletion. Orphan classes are evidence only.
3. **Missing-vector repair.** `vectors_synced` is a hub-wide flag on a
   content version, and adoption trusts it. Apply therefore resets the flag
   on every version that has `vectors_synced = true` and at least one symbol
   point missing, whichever machine references it, including none. Versions
   with the flag already false are pending: they are reported and left alone.
   The daemon sync worker re-embeds; reconcile never embeds.
4. **Completion ownership.** The gcode completion compare-and-set owns
   `vectors_synced = true`. The Python worker stops marking vectors synced
   (`sync_worker.py:583`). Its attempt mark (`sync_worker.py:497`) stays,
   because it only clears the flag; the native command marks again and
   captures the attempt it completes.
5. **Re-parse reset.** Writing facts for an existing version compares the
   stored vector-text map before and after the write. When they differ, it
   resets that version's vector flag in the same transaction.
6. **`vector cleanup-orphans` is removed.** Its path-level authority deletes
   the points of retained versions, and its payload-filtered scroll misses
   legacy points that carry no payload. `graph cleanup-orphans` is unchanged.
7. **Evidence and apply guard.** The dry-run prints the full inventory as JSON
   on stdout. Text format prints counts and the digest. `--apply` requires
   `--expect-digest` and `--receipt`. A digest mismatch under the lease
   refuses with no mutation. The receipt path follows the retire-files
   location rules.
8. **Detached writers.** A projection worker that outlives its lock only
   upserts IDs it read from PostgreSQL. Reconcile never deletes an ID present
   in PostgreSQL under the lease. A point recreated after a pass is an orphan
   for the next pass. No fencing is added.
9. **History protection.** It keys on `(path, content_hash)`, gathered by one
   `cat-file` tree walk pruned to candidate paths. When history cannot be
   read, every candidate of that root is retained.
10. **Eligibility.** A candidate whose path is ineligible under the root's
    current config is collected whatever its history. A missing path is never
    treated as gitignore-excluded.
11. **Automation.** An hourly automated apply is deferred (D1) until the live
    procedure has run once.
12. **Routed outside this plan** (to the Orchestrator):
    - The graph CLI's `sync_file` (`commands/graph/lifecycle.rs`) ignores the
      `mark_graph_synced` result and returns Synced. The Python worker then
      marks `current.id` graph-synced, which is the same race as S2.
    - `graph_synced` is not reset when a same-hash re-parse rewrites graph
      facts.

## As-Is Facts
`kind: framing`

Source mechanisms at `cf88559ef2`:
- **Identity.** `Symbol::make_id` (`crates/gcode/src/models.rs:159-170`) is a
  UUID5 over `project:path:file_content_hash:name:kind:byte_start`. Before
  #19743 the key was `project:path:name:kind:byte_start`. A Qdrant point ID
  equals its symbol ID, and each project has one collection,
  `code_symbols_{project_id}`. The payload (`vector/code_symbols/types.rs:28-57`)
  carries `project_id`, `file_path`, `symbol_id`, `name`, `kind` and
  `byte_start`, but no content hash.
- **Write and delete paths.** Vector sync is upsert-only
  (`vector/code_symbols/lifecycle.rs`). Points are deleted only by:
  - content GC `delete_candidate_projections` (`content_gc.rs:288-311`);
  - `retire-files`, from #21872 (Retire exact inventoried code-index file
    versions and projections);
  - `vector clear` and collection drop;
  - `vector cleanup-orphans`.

  `projection::reconcile_deleted_file` is a no-op. For a deleted file, the
  indexer only calls `api::file_state::delete_file_state`.
- **Fact write order.** `write_parsed_file_facts` (`index/indexer/file.rs:158-217`):
  1. `upsert_file`: `ON CONFLICT(id)` keeps both sync flags (`index/api.rs:267-291`).
  2. `upsert_symbols`.
  3. `delete_stale_file_symbols`: PostgreSQL only.
  4. `delete_file_non_symbol_facts`.
  5. Imports, calls and inheritance.
  6. Chunks.

  A new version is inserted with false and NULL flags. A `--full` re-parse
  skips adoption (`index/indexer/pipeline.rs:152-167`).
- **Completion marks.** `mark_vector_sync_attempted` and
  `mark_vectors_synced` (`db/queries.rs:235-275`) match on project and path
  through this machine's file state. Neither checks a hash or an attempt.
  The graph pair is a compare-and-set: `mark_graph_sync_attempted` returns
  `GraphSyncAttempt`, and `mark_graph_synced` dirties the live row on
  failure (`db/queries.rs:13-186`).
- **Callers of the marks.**
  - `projection/sync.rs::VectorProjectionState::sync_file` (646-661) runs
    inside a bounded worker. The caller abandons a stalled worker after 300
    seconds and releases its lock (280-342).
  - CLI `vector sync-file` (`commands/vector.rs:43-85`) holds the file lock.
  - The Python worker `_sync_file` reads `current`, then
    `mark_vector_sync_attempted(current.id)` (:497), then runs gcode with
    `--allow-missing-indexed-file`, then
    `mark_vectors_synced(current.id, current.content_hash)` (:583).
  - `_storage/files.py::mark_vectors_synced` is the only Python mark.
- **Locks.** File writers take a shared project key plus an exclusive file
  key. `lease_project_lock(..., IndexLockPolicy::maintenance_try())` takes the
  exclusive project key. Content GC (`content_gc.rs:201`) and retire-files
  (`retire_files.rs:81`) take that lease. PostgreSQL advisory locks are
  hub-wide.
- **Search.** `semantic_search` fetches four times the limit, then post-filters
  through visibility. Orphan points never hydrate, but they consume the
  overfetch window, so they cost recall.
- **`vector cleanup-orphans`** (`qdrant.rs:132-157`, `commands/vector.rs:175-194`):
  - It scrolls payloads filtered by `project_id` and deletes every path
    absent from this machine's indexed set.
  - The contract marks it `daemon_consumed: true` (`contract.rs:525`), but no
    Python caller exists.
  - Its references are `graphs.md:42`, the user guide, the graph-core guide,
    `code-index.md:192`, the gcode README and `docs/reference-audit/code-index.json`.
  - `gcode prune` runs no vector orphan sweep, although
    `gcode-graph-core.md:131-137` and `gcode-user-guide.md:465-468` say it
    does.
- **Content GC.** `discover_content_gc(database_url, retention_days,
  project_filter)` (`content_gc.rs:42-152`) has one production caller,
  `prune.rs:215`, which is always project-scoped. A candidate is a version:
  - no machine's file state references it;
  - its `last_referenced_at` is older than the retention window;
  - its root is this machine's project-state `root_path`.

  `recent_content_hashes_in_git_history` (374-493) runs
  `rev-list --objects --all --since` (one name per object, so paths are
  lossy), then `cat-file --batch --filters`. It matches on content hash only
  and retains everything on failure. The daemon's hourly prune passes
  `config.content_retention_days` (`src/gobby/code_index/prune.py:264`).
  `gcode prune` without `--force` prints every candidate to stderr before its
  `[y/N]` prompt (`prune.rs:223-247`).
- **Classifier.** `classify_file` needs the file on disk.
  `passes_path_filters` and the `is_hidden_path` allowlist are lexical.
  `explicit_path_visible` (`walker/classification.rs:90-120`, private) walks
  the filesystem. Excludes come from `effective_excludes`
  (`index/indexer/util.rs:32`, `pub(super)`) over
  `ctx.indexing.extra_excludes`, and the gitignore switch is
  `ctx.indexing.respect_gitignore`.
- **Contract.** `contract_version` is 12 (`contract.rs:12`,
  `docs/contracts/gcode-cli.md:8`). Commands are bound by
  `tests/skills/reference_library_helpers.py::native_cli_inventory`: the
  `contract.rs` command names, `tests/contracts/gcode.contract.json`, the
  `cli.rs` enums and `docs/reference-audit/code-index.json` must agree.
- **CI.** The Rust CI job runs DB-gated gcode tests only when the test name
  matches `serial_db` (`.github/workflows/rust-ci.yml:264`).
  `db/queries_cas_tests.rs` sits outside that filter.

Measured, from the archive report (2026-09-26; historical, not re-measured):
- 261,954 Qdrant points against 183,097 PostgreSQL symbols.
- 91,028 orphan points, 81,839 of them carrying a path. 54,359 match the
  legacy ID scheme. 27,480 content-addressed orphans have inferred causes.
  9,189 have no provenance.
- 70,571 orphans duplicate a live path, name and kind.
- 12,171 symbols lack a vector.
- Content GC: the report states 1,314 retained versions, while its buckets
  sum to 1,451. The difference is unreconciled.

Source-proven mechanisms (frequency unmeasured):
- **S1.** A same-version re-parse can change symbol IDs while keeping
  `vectors_synced = true`. The new IDs are never embedded, and the old points
  are orphaned.
- **S2.** The Python worker marks the version it read (`current.id`) as
  synced, after the native command synced whatever was current under its
  lock. If the state moved from H1 to H2 in between, H1 is marked synced with
  no vectors. A later adoption of H1 then exposes missing vectors.
- **S3.** An abandoned bounded projection worker can finish after its lock is
  released and mark the new current version synced.
- **S4.** `vector cleanup-orphans` deletes the points of retained versions
  whose path is absent here, and misses points without a payload.
- **S5.** Content GC history protection ignores paths. Any path in history
  with equal content protects a candidate. History also protects paths now
  excluded by config.

Hypotheses and the evidence that settles each:

| Hypothesis | Evidence |
|---|---|
| The 54,359 legacy-scheme orphans are pre-#19743 points that nothing removed | 2.1 class `legacy_id_scheme` |
| The 9,189 points without provenance carry pre-payload-schema payloads | 2.1 class `legacy_payload` |
| The 27,480 content-addressed orphans come from S1 | 2.1 class `reparsed_version`; the remainder is `unattributed` |
| The 12,171 missing vectors come from S1 to S4 | 2.1 classes `synced_missing_current` and `synced_missing_retained` |
| The 1,314 versus 1,451 GC figures describe one candidate set | Live procedure step 0 preview counts, current and candidate binaries |

## Constraints
`kind: framing`

- Main checkout, branch `0.5.0`. No schema migration and no new hub table.
  The flag reset uses existing columns.
- Monolith ceiling. Planned edits keep these production files below the
  850-line decomposition trigger:
  - `db/queries.rs` (664 lines);
  - `projection/sync.rs` (716);
  - `index/api.rs` (716, untouched);
  - `vector/code_symbols/embedding.rs` (752, untouched);
  - `cli.rs` (657), `contract.rs` (647), `dispatch.rs` (598);
  - `retire_files.rs` (558), `qdrant.rs` (553);
  - `src/gobby/code_index/sync_worker.py` (728).

  New logic goes in new modules: `commands/vector/reconcile.rs`,
  `commands/receipt.rs` and `commands/status/content_gc/history.rs`.
  `src/gobby/code_index/gcode_gateway.py` (950) is not touched.
- Contract version. 2.1 bumps 12 to 13, and 2.2 extends the same version
  note. If another change takes 13 first, 2.1 takes the next free version and
  records it in the `gcode-cli.md` note.
- DB tests use `#[cfg_attr(not(gcode_postgres_tests), ignore = "...")]` and
  `#[serial_test::serial(serial_db)]`. New DB tests live in a module whose
  path contains `serial_db`.
- #23552 (Run every DB-gated gcode test module in CI) is the work that
  brings `db/queries_cas_tests.rs` under CI. Until it lands, the
  vector-completion tests there run through each leaf's planned verification
  command. If #23552 has moved that module, 1.1 and 1.3 follow its layout.
- Embedding cost. The missing-vector repair re-embeds at most the symbols of
  the reset versions, through the daemon's normal worker and breakers.

## P1: Stop New Drift
`kind: framing`

**Goal:** completion is a compare-and-set owned by gcode, and a re-parse can
no longer leave new symbol IDs unembedded behind a true flag.

### 1.1 Vector completion compare-and-set [category: code]
`kind: deliverable`

Targets:
- `crates/gcode/src/db/queries.rs::mark_vector_sync_attempted`
- `crates/gcode/src/db/queries.rs::mark_vectors_synced`
- `crates/gcode/src/projection/sync.rs::VectorProjectionState`
- `crates/gcode/src/commands/vector.rs::sync_file`
- `crates/gcode/src/index/api_tests.rs::*` — scope-reason: the attempt and completion calls at 111-133 move to the new signatures
- `crates/gcode/src/db/queries_cas_tests.rs::*` — scope-reason: add vector compare-and-set tests beside the graph ones
- `crates/gcode/src/projection/sync/tests.rs::*` — scope-reason: add a `serial_db` module with the superseded-completion test
- `crates/gcode/src/vector/code_symbols.rs::*` — scope-reason: the `cfg(test)` `mod tests` declaration becomes `pub(crate)` so the projection and reconcile tests reach its HTTP helpers
- `crates/gcode/src/vector/code_symbols/tests.rs::*` — scope-reason: `accept_with_timeout` and `read_http_request` become `pub(crate)` for the projection and reconcile tests

Consumers unchanged:
- `crates/gcode/src/commands/graph/lifecycle.rs` — no-edit-reason: the graph-side analog is routed to the Orchestrator (Decision Record 12).
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: it already passes `--allow-missing-indexed-file`, and the skip payload keys are unchanged.
- `crates/gcode/src/vector/code_symbols/lifecycle.rs` — no-edit-reason: upsert batching is unchanged.

**Research context:** see As-Is Facts (completion marks, callers, locks). The
graph compare-and-set in `db/queries.rs:13-186` and its tests in
`db/queries_cas_tests.rs` are the pattern. The project-level
`mark_project_vector_sync_attempted` and `mark_project_vectors_synced` serve
`vector rebuild` under the exclusive project lock and stay as they are.

Implementation:
- Add `pub struct VectorSyncAttempt { content_hash: String, attempted_at: ... }`
  beside `GraphSyncAttempt`, with the same field types.
- `mark_vector_sync_attempted(conn, project, path)` returns
  `Option<VectorSyncAttempt>`. It sets `vectors_synced = false` and
  `vector_sync_attempted_at = NOW()` on this machine's current version and
  returns that version's hash and timestamp.
- `mark_vectors_synced(conn, project, path, content_hash, attempted_at) -> bool`
  sets `vectors_synced = true`, keeping `vector_sync_attempted_at`, only on
  the row whose hash and attempt both match and which this machine's state
  still references. When nothing matches, a CTE in the same statement sets
  `vectors_synced = false` and `vector_sync_attempted_at = NULL` on this
  machine's current row for the path, and the call returns false. This
  mirrors `mark_graph_synced`.
- `VectorProjectionState::sync_file` captures the attempt before fetching
  symbols and passes it to the compare-and-set. A failed compare-and-set
  returns `ProjectionFileSyncOutcome::SkippedMissingIndexedFile`.
- `commands/vector.rs::sync_file` does the same. A failed compare-and-set
  prints the existing skipped payload with `"reason": "sync_superseded"` and
  summary `skipped vector sync for <path>: superseded by a newer index`. It
  skips `projection::reconcile_deleted_file` and exits 0, with or without
  `--allow-missing-indexed-file`. A missing row on the attempt keeps today's
  `indexed_file_not_found` behavior.
- The superseded-completion test runs a scripted HTTP server for embedding
  and Qdrant. Its handler points this machine's state at a second hash
  before answering the upsert.

Planned verification:
`GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test cargo nextest run -p gobby-code -E 'test(mark_vectors_synced) | test(mark_vector_sync) | test(serial_db) | test(api_tests)'`,
then `cargo clippy -p gobby-code` and `cargo fmt -p gobby-code -- --check`.

**Acceptance:**

- 1.1.1 - An attempt returns the current hash and timestamp. Completion
  with that attempt marks the version synced and keeps its attempt time.
  test: `crates/gcode/src/db/queries_cas_tests.rs::mark_vectors_synced_cas_marks_attempted_version`.
- 1.1.2 - After this machine's state moves to another hash, completion with
  the old attempt returns false. It leaves the old row unsynced and sets the
  new current row to `vectors_synced = false` with a NULL attempt. test:
  `crates/gcode/src/db/queries_cas_tests.rs::mark_vectors_synced_cas_rejects_completion_after_state_moves`.
- 1.1.3 - A second attempt on the same hash invalidates the first attempt's
  completion. test:
  `crates/gcode/src/db/queries_cas_tests.rs::mark_vectors_synced_cas_rejects_same_hash_stale_attempt`.
- 1.1.4 - When the state moves during the upsert, `VectorProjectionState`
  reports a skip and the new current version stays pending. test:
  `crates/gcode/src/projection/sync/tests.rs::vector_sync_file_skips_when_state_moves_during_upsert`.

### 1.2 Sync worker leaves vector completion to gcode [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `src/gobby/code_index/sync_worker.py::_sync_file`
- `src/gobby/code_index/_storage/files.py::CodeIndexFileStorageMixin`
- `tests/code_index/test_sync_worker.py::*` — scope-reason: drop `mark_vectors_synced` assertions and fakes, add the two completion tests
- `tests/code_index/test_sync_worker_breaker.py::*` — scope-reason: drop `mark_vectors_synced` fakes and assertions
- `tests/test_runner_code_index_shutdown.py::*` — scope-reason: its storage fake defines `mark_vectors_synced`
- `tests/code_index/test_code_index_storage.py::test_stale_content_hash_rejects_sync_marks_and_summary`

Consumers unchanged:
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: `vector_sync_file` already returns the native payload; 1.1 owns the native skip payload.
- `src/gobby/code_index/storage.py` — no-edit-reason: composes the mixin; no caller of the removed method remains after 1.2.

**Research context:** `_sync_file` (`sync_worker.py:466-689`) treats a
native result that passes `_require_projection_success` as terminal, then
marks `current.id` synced at :583. `_storage/files.py:211` is the only
definition, and the test references sit in the four test files above.

Implementation:
- Delete the `storage.mark_vectors_synced` call at :583. Keep breaker
  bookkeeping and `did_work = True`.
- Delete `CodeIndexFileStorageMixin.mark_vectors_synced` and every test
  reference and fake.
- Keep `mark_vector_sync_attempted` (:497) and `requeue_vector_sync`.

Planned verification:
`DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_sync_worker.py tests/code_index/test_sync_worker_breaker.py tests/test_runner_code_index_shutdown.py tests/code_index/test_code_index_storage.py -q`,
then `uv run ruff check src/ && uv run mypy src/`.

**Acceptance:**

- 1.2.1 - A successful native vector result leaves `vectors_synced` as gcode
  wrote it. The worker never sets it, and the storage mixin has no
  `mark_vectors_synced`. test:
  `tests/code_index/test_sync_worker.py::test_vector_sync_leaves_completion_to_native_cas`.
- 1.2.2 - A native `{"status": "skipped", "reason": "sync_superseded"}`
  result raises no error and leaves the file pending for the next scan.
  test: `tests/code_index/test_sync_worker.py::test_superseded_vector_skip_keeps_file_pending`.

### 1.3 Re-parse vector reset [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/index/indexer/file.rs::write_parsed_file_facts`
- `crates/gcode/src/index/indexer/sink.rs::CodeFactSink`
- `crates/gcode/src/index/indexer/sink.rs::PostgresCodeFactSink`
- `crates/gcode/src/db/queries.rs::read_symbols_for_file`
- `crates/gcode/src/vector/code_symbols.rs::*` — scope-reason: re-export `vector_text_for_symbol` as `pub(crate)` outside `cfg(test)`
- `crates/gcode/src/db/queries_cas_tests.rs::*` — scope-reason: add the version-dirty query test
- `crates/gcode/src/index/indexer/tests/facts.rs::*` — scope-reason: `RecordingCodeFactSink` implements the two new trait methods; add the reset unit tests
- `crates/gcode/src/index/indexer/tests/serial_db.rs::*` — scope-reason: add the full re-index test
- `docs/guides/gcode-development-guide.md`

Consumers unchanged:
- `crates/gcode/src/index/indexer/pipeline.rs` — no-edit-reason: adoption and `--full` routing are unchanged.
- `crates/gcode/src/vector/code_symbols/embedding.rs` — no-edit-reason: `vector_text_for_symbol` is already `pub`.

**Research context:** see As-Is Facts (fact write order). `read_symbols_for_file`
(`db/queries.rs:390`, private) reads one version's symbols over any
`GenericClient`, and `db/mod.rs` re-exports `queries::*`. Vector text
(`embedding.rs:401-432`) covers name, qualified name, kind, language, path,
range, signature, docstring and summary. Upserts preserve stored summaries.

Implementation:
- `read_symbols_for_file` becomes `pub(crate)`.
- New `pub(crate) fn dirty_vector_sync_for_version(conn, project_id, file_path, content_hash) -> anyhow::Result<bool>`
  runs `UPDATE code_indexed_files SET vectors_synced = false, vector_sync_attempted_at = NULL`
  on that version row, with no file-state join.
- `CodeFactSink` gains `read_version_symbols(project_id, file_path, content_hash) -> Vec<Symbol>`
  and `dirty_vector_sync_for_version(project_id, file_path, content_hash)`.
  `PostgresCodeFactSink` delegates both to `db`.
- `write_parsed_file_facts` reads `before` first.
  - When `before` is empty, it dirties only if the parse produced symbols.
  - Otherwise it re-reads the symbols after the chunk write and compares
    `BTreeMap<id, vector_text_for_symbol>` of before and after. It dirties
    when they differ.
  - The reset joins the sink's transaction. `FileIndexCounts` is unchanged.
- In `gcode-development-guide.md` item 5, a re-parse of an existing version
  keeps its sync flags unless the stored vector text changed. When it did,
  the vector flag resets to pending.

Planned verification:
`GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test cargo nextest run -p gobby-code -E 'test(facts) | test(serial_db) | test(dirty_vector_sync)'`,
then `cargo clippy -p gobby-code` and `cargo fmt -p gobby-code -- --check`.

**Acceptance:**

- 1.3.1 - Re-writing a version whose symbol set changes dirties its vector
  flag once. test:
  `crates/gcode/src/index/indexer/tests/facts.rs::reparse_with_changed_vector_text_dirties_version`.
- 1.3.2 - Re-writing a version with identical symbols leaves the flag
  alone. test:
  `crates/gcode/src/index/indexer/tests/facts.rs::reparse_with_identical_symbols_keeps_vector_flag`.
- 1.3.3 - An empty stored set with an empty parse does not dirty. test:
  `crates/gcode/src/index/indexer/tests/facts.rs::empty_reparse_of_empty_version_keeps_vector_flag`.
- 1.3.4 - A non-empty stored set with an empty parse dirties. test:
  `crates/gcode/src/index/indexer/tests/facts.rs::reparse_that_drops_all_symbols_dirties_version`.
- 1.3.5 - A `--full` re-index that changes symbol IDs of a synced version
  leaves that version `vectors_synced = false` with a NULL attempt. test:
  `crates/gcode/src/index/indexer/tests/serial_db.rs::full_reindex_with_changed_symbol_ids_marks_vectors_pending`.
- 1.3.6 - `dirty_vector_sync_for_version` changes only the named version,
  even when no file state references it. test:
  `crates/gcode/src/db/queries_cas_tests.rs::dirty_vector_sync_for_version_touches_only_that_version`.

## P2: Vector Reconcile
`kind: framing`

**Goal:** one command proves the vector projection matches PostgreSQL for a
project. It deletes only what PostgreSQL disowns and returns missing vectors
to the sync worker, under the lease and with a receipt.

### 2.1 `gcode vector reconcile` dry-run [category: code] (depends: 1.3)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/vector/reconcile.rs`
- `crates/gcode/src/commands/vector/reconcile/tests.rs`
- `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs`
- `crates/gcode/src/commands/vector.rs::cleanup_orphans`
- `crates/gcode/src/commands/vector.rs::print_orphan_cleanup`
- `crates/gcode/src/vector/code_symbols/qdrant.rs::cleanup_orphan_file_vectors`
- `crates/gcode/src/vector/code_symbols/qdrant.rs::list_project_vector_file_paths`
- `crates/gcode/src/vector/code_symbols/qdrant.rs::collect_file_paths_from_scroll_page`
- `crates/gcode/src/vector/code_symbols/qdrant.rs::VectorOrphanCleanup`
- `crates/gcode/src/vector/code_symbols/qdrant.rs::delete_file_vectors`
- `crates/gcode/src/vector/code_symbols.rs::*` — scope-reason: swap the cleanup re-exports for `scroll_point_identities` and `PointIdentity`
- `crates/gcode/src/vector/code_symbols/tests/deletion.rs::*` — scope-reason: remove the three tests of the deleted functions
- `crates/gcode/src/cli.rs::VectorCommand`
- `crates/gcode/src/dispatch.rs::*` — scope-reason: the service-selection arm (:153) and the command arm (:516) move to `Reconcile`
- `crates/gcode/src/contract.rs::*` — scope-reason: replace the `vector cleanup-orphans` entry and bump `contract_version`
- `crates/gcode/src/contract/schema.rs::*` — scope-reason: `vector_cleanup_keys` becomes `vector_reconcile_keys`
- `crates/gcode/contract/gcode.contract.json::*` — scope-reason: regenerated from `gcode contract`; the `vector reconcile` entry replaces `vector cleanup-orphans` and the version becomes 13
- `tests/contracts/gcode.contract.json::*` — scope-reason: the drift-test copy of the same regenerated contract
- `docs/contracts/gcode-cli.md`
- `docs/reference-audit/code-index.json::*` — scope-reason: the `vector cleanup-orphans` audit entry becomes `vector reconcile` (operator, `recovery.md`)
- `src/gobby/install/shared/skills/gobby/references/code-index/graphs.md`
- `src/gobby/install/shared/skills/gobby/references/code-index/recovery.md`
- `crates/gcode/tests/contract.rs::contract_is_version_twelve_without_ask`
- `crates/gcode/src/cli/tests/projection.rs::*` — scope-reason: the parse test for `vector cleanup-orphans` becomes `vector reconcile`
- `crates/gcode/src/cli/tests/top_level.rs::*` — scope-reason: the command list at :384 names `reconcile`
- `crates/gcode/src/dispatch/tests.rs::*` — scope-reason: the service test at :71 names `reconcile`

Consumers unchanged:
- `tests/skills/reference_library_helpers.py` — no-edit-reason: it reads the contract, enums and audit generically.
- `tests/skills/test_reference_library.py` — no-edit-reason: it asserts the binding these targets keep consistent.
- `crates/gcode/src/commands/graph/lifecycle.rs` — no-edit-reason: `graph cleanup-orphans` stays.
- `tests/ai/test_tool_chat_tools.py` — no-edit-reason: its `cleanup-orphans` entry is the graph command.
- `crates/CHANGELOG.md` — no-edit-reason: historical entries.
- `crates/gcode/tests/fixtures/retired-code-index-skill.md` — no-edit-reason: a retired fixture.

**Research context:** see As-Is Facts (identity, `vector cleanup-orphans`,
contract). The dispatch currently selects `ServiceConfigSelection::qdrant_only()`
for cleanup. Context resolution performs no DB writes. Scroll paging and
404 handling follow `list_project_vector_file_paths`. Tests use a scripted
Qdrant through `vector/code_symbols/tests.rs::spawn_http_responses`, and
PostgreSQL through the `serial_db` fixture pattern of
`commands/status/content_gc/tests.rs`.

Implementation:
- **Command.** `VectorCommand::CleanupOrphans` becomes `VectorCommand::Reconcile`.
  It is invoked as `gcode [--project <root>] vector reconcile [--format text|json]`,
  dispatches with `qdrant_only()` and does not run `ensure_project_fresh`.
  `commands/vector.rs` declares `pub(crate) mod reconcile;` and routes to
  `reconcile::run(ctx, format)`.
- **Qdrant scroll.** `qdrant.rs::scroll_point_identities(qdrant, project_id) -> Vec<PointIdentity>`
  replaces `list_project_vector_file_paths` and
  `collect_file_paths_from_scroll_page`. It scrolls the collection
  unfiltered, without vectors, with payload fields `project_id`, `file_path`,
  `name`, `kind` and `byte_start`. A missing collection yields an empty
  list.
- **Removals.** Delete `cleanup_orphan_file_vectors`, `VectorOrphanCleanup`,
  `delete_file_vectors` (test-only after the removal),
  `commands/vector.rs::cleanup_orphans` and `print_orphan_cleanup`.
- **Inventory** (`reconcile.rs`, read-only PostgreSQL connection):
  - `points`: the scrolled identities.
  - `pg_ids`: `SELECT id FROM code_symbols WHERE project_id = $1`.
  - `versions`: each `code_indexed_files` row of the project, with
    `vectors_synced`, its symbol IDs, and whether any machine's
    `code_indexed_file_states` row references it.
- **Orphans** are point IDs not in `pg_ids`. Each falls in the first class
  that matches:
  1. `legacy_payload`: no `file_path` in the payload.
  2. `legacy_id_scheme`: the ID equals the UUID5 of
     `project:path:name:kind:byte_start` under `CODE_INDEX_UUID_NAMESPACE`.
  3. `reparsed_version`: `Symbol::make_id` for some PostgreSQL version hash
     of that path equals the ID.
  4. `unattributed`: none of the above.
- **Missing** covers versions with at least one symbol ID absent from the
  points:
  - `synced_missing_current`: flag true and referenced by a state.
  - `synced_missing_retained`: flag true and referenced by none.
  - `pending`: flag false.
- **Digest.** `inventory_digest` is `sha256:` plus the hex SHA-256 of the
  sorted orphan IDs and the sorted synced-missing version IDs. The two
  groups are each newline-terminated and separated by one `--` line.
- **JSON output.** Per orphan class: `count` and `ids`. Per missing class:
  `versions`, `symbols` and `version_ids`. Also `points_scanned`,
  `pg_symbols`, `inventory_digest`, `mode: "dry_run"` and `success`. Text
  output prints the counts and the digest.
- **Contract.** The entry is `vector reconcile` with `daemon_consumed: false`,
  flags `--format`, and the new keys. `contract_version` goes to 13, with a
  `gcode-cli.md` version note. Both contract JSON files are regenerated from
  `gcode contract --format json`. The test
  `contract_is_version_twelve_without_ask` becomes
  `contract_is_version_thirteen_without_ask`.
- **References.** The audit entry for `vector reconcile` gets audience
  `operator` and reference `recovery.md`. `graphs.md:42` names `vector
  reconcile` as the read-only inventory. `recovery.md` gives the dry-run
  command and says deletion needs the reviewed apply procedure.

**Granularity:** eight production Rust files, one outcome: the command
surface swap. `test_reference_library` binds `contract.rs`, `cli.rs`, the
contract JSON and the audit JSON, so a split leaves an intermediate commit
that fails it. The Qdrant scroll and removals have no other consumer.

Planned verification:
`cargo nextest run -p gobby-code`, then
`GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test cargo nextest run -p gobby-code -E 'test(reconcile)'`,
then `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_reference_library.py -q`,
then `cargo clippy -p gobby-code` and `cargo fmt -p gobby-code -- --check`.

**Acceptance:**

- 2.1.1 - Only point IDs absent from PostgreSQL are orphans, and each lands
  in its class (four seeded points, one per class, plus live points). test:
  `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs::dry_run_classifies_only_ids_absent_from_postgres`.
- 2.1.2 - Synced versions missing points are split by state reference;
  versions with the flag false report as pending. test:
  `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs::dry_run_splits_missing_vectors_by_flag_and_reference`.
- 2.1.3 - The digest does not depend on scroll order and changes when any
  orphan or synced-missing version changes. test:
  `crates/gcode/src/commands/vector/reconcile/tests.rs::inventory_digest_is_order_independent_and_content_sensitive`.
- 2.1.4 - A dry-run sends only scroll requests to Qdrant and leaves every
  PostgreSQL flag unchanged. test:
  `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs::dry_run_mutates_nothing`.
- 2.1.5 - A missing collection reports zero points, and every symbol is
  missing. test:
  `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs::dry_run_treats_missing_collection_as_empty`.
- 2.1.6 - The contract is version 13 without `vector cleanup-orphans`, and
  the contract, CLI and audit agree on `vector reconcile`. test:
  `crates/gcode/tests/contract.rs::contract_is_version_thirteen_without_ask`.

### 2.2 Reconcile apply under the maintenance lease [category: code] (depends: 2.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/vector/reconcile.rs`
- `crates/gcode/src/commands/vector/reconcile/tests.rs`
- `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs`
- `crates/gcode/src/commands/receipt.rs`
- `crates/gcode/src/commands/mod.rs::*` — scope-reason: declare `pub(crate) mod receipt;`
- `crates/gcode/src/commands/status/retire_files.rs::validate_receipt_path`
- `crates/gcode/src/commands/status/retire_files.rs::write_receipt`
- `crates/gcode/src/commands/status/retire_files/manifest.rs::no_symlinks`
- `crates/gcode/src/commands/status/retire_files/manifest.rs::require_private_file`
- `crates/gcode/src/cli.rs::VectorCommand`
- `crates/gcode/src/dispatch.rs::*` — scope-reason: the `Reconcile` arm passes the new flags
- `crates/gcode/src/contract.rs::*` — scope-reason: the `vector reconcile` entry gains three flags
- `crates/gcode/src/contract/schema.rs::*` — scope-reason: `vector_reconcile_keys` adds the apply keys
- `crates/gcode/contract/gcode.contract.json::*` — scope-reason: regenerated from `gcode contract`; the `vector reconcile` entry gains the apply flags and keys
- `tests/contracts/gcode.contract.json::*` — scope-reason: the drift-test copy of the same regenerated contract
- `docs/contracts/gcode-cli.md`
- `src/gobby/install/shared/skills/gobby/references/code-index/recovery.md`
- `crates/gcode/src/cli/tests/projection.rs::*` — scope-reason: parse tests for `--apply` requiring `--expect-digest` and `--receipt`

Consumers unchanged:
- `crates/gcode/src/commands/status/retire_files/tests.rs` — no-edit-reason: retire-files behavior and receipt identity checks are unchanged.
- `crates/gcode/src/commands/status/retire_files/tests/serial_db.rs` — no-edit-reason: same receipts at the same paths.
- `crates/gcode/src/index_lock.rs` — no-edit-reason: `lease_project_lock` and `maintenance_try()` are reused; content GC takes the same lease, so GC and apply exclude each other, and `delete_symbol_vectors` (wait=true) is reused unchanged.

**Research context:** retire-files is the precedent. It takes the
`maintenance_try()` lease and fails with "project index is busy" when the
lease is held, deletes projections in batches of `PROJECTION_BATCH_SIZE = 256`,
and writes its receipt atomically. `validate_receipt_path` checks:
- no symlinks;
- a canonical path;
- a location outside the repository root;
- a private parent owned by the caller;
- a private existing file;
- a matching previous retire-files receipt identity.

`write_receipt` writes a temp file, syncs and persists it, then syncs the
parent directory.

Implementation:
- **Shared receipt module.** `commands/receipt.rs` holds:
  - `pub(crate) fn validate_location(path, root_path)`: every location
    check above;
  - `pub(crate) fn read_existing<T: DeserializeOwned>(path) -> Option<T>`;
  - `pub(crate) fn write_atomic<T: Serialize>(path, &T)`.

  `no_symlinks` and `require_private_file` move there from
  `retire_files/manifest.rs`, which imports them. Retire-files keeps its
  previous-receipt identity check and calls the shared functions.
- **Flags.** `--apply` requires `--expect-digest <sha256:...>` and
  `--receipt <file>` (clap `requires`). `--receipt` without `--apply` is
  refused.
- **Apply sequence.**
  1. Validate the receipt location.
  2. Open a read-write connection and take
     `lease_project_lock(maintenance_try())`. Busy fails with "project index
     is busy; rerun the dry-run" and makes no mutation and no receipt.
  3. Rebuild the inventory under the lease. A digest that differs from
     `--expect-digest` refuses with both digests and makes no mutation.
  4. In one transaction, run `UPDATE code_indexed_files SET vectors_synced = false, vector_sync_attempted_at = NULL WHERE id = ANY($1) AND vectors_synced`
     for the synced-missing version IDs.
  5. Write the receipt: digest, project, collection, `version_ids_reset`,
     orphan IDs per class, `deleted: []`, `complete: false`.
  6. Delete orphan IDs with `delete_symbol_vectors` in batches of 256. Append
     each batch to `deleted` and rewrite the receipt after it.
  7. On success, set `complete: true` and exit 0.
- **Failure.** A Qdrant error stops the batch loop, leaves `complete: false`,
  prints the error with the receipt path, and exits nonzero. A rerun starts
  over from the dry-run.
- **Existing receipt.** A receipt already present for the same project,
  collection and digest is overwritten by the rerun. A receipt for a
  different digest is refused.
- **Output.** The JSON keys add `mode: "apply"`, `receipt`, `deleted_points`,
  `versions_reset` and `complete`. The `gcode-cli.md` version-13 note
  extends to the apply flags. `recovery.md` gives the apply command and
  states that it runs only through the Live Maintenance Procedure.

**Granularity:** nine production files, one outcome: guarded apply. The
receipt extraction exists only so apply and retire-files share one set of
location checks. Shipping it alone leaves no consumer, and shipping apply
without it duplicates security checks.

Planned verification:
`GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test cargo nextest run -p gobby-code -E 'test(reconcile) | test(retire_files) | test(projection)'`,
then `DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/skills/test_reference_library.py -q`,
then `cargo clippy -p gobby-code` and `cargo fmt -p gobby-code -- --check`.

**Acceptance:**

- 2.2.1 - Apply with the dry-run digest deletes exactly the orphan IDs,
  resets exactly the synced-missing versions, and writes a complete receipt.
  An immediate dry-run reports zero orphans and zero synced-missing
  versions. test:
  `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs::apply_deletes_only_disowned_ids_and_writes_receipt`.
- 2.2.2 - With the maintenance lease held elsewhere, apply fails busy and
  sends no Qdrant delete, changes no flag and writes no receipt. test:
  `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs::apply_is_busy_without_mutation_when_lease_is_held`.
- 2.2.3 - When a PostgreSQL symbol gains an orphan's ID between the dry-run
  and the apply, the inventory rebuilt under the lease no longer matches,
  and apply refuses without mutation. test:
  `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs::apply_refuses_when_inventory_changes_under_lease`.
- 2.2.4 - A Qdrant delete failure leaves a receipt with `complete: false`
  listing the deleted batches, and the exit is nonzero. test:
  `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs::apply_records_partial_receipt_on_qdrant_failure`.
- 2.2.5 - A receipt path inside the repository root, or under a non-private
  parent, is refused before the lease. test:
  `crates/gcode/src/commands/vector/reconcile/tests.rs::apply_refuses_receipt_outside_private_location`.
- 2.2.6 - The flag reset leaves pending versions and other projects'
  versions unchanged. test:
  `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs::apply_resets_only_synced_missing_versions`.

## P3: Path-Aware Content Retention
`kind: framing`

**Goal:** content GC keeps a version only for history at its own path, and
only while that path is still indexable.

### 3.1 Path-keyed history protection [category: code]
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/status/content_gc/history.rs`
- `crates/gcode/src/commands/status/content_gc/history/tests.rs`
- `crates/gcode/src/commands/status/content_gc.rs::discover_content_gc`
- `crates/gcode/src/commands/status/content_gc.rs::recent_content_hashes_in_git_history`
- `crates/gcode/src/commands/status/content_gc/tests.rs::*` — scope-reason: replace `recent_git_blob_protects_matching_content` and add the path-level discovery test

Consumers unchanged:
- `src/gobby/code_index/prune.py` — no-edit-reason: the hourly invocation and budget are unchanged.

**Research context:** see As-Is Facts (content GC). Git 2.54 supports
`cat-file --batch -Z --filters`. Measured on this repository, a 30-day window
holds 4,188 unique root trees and 9,670 files, so one `ls-tree` per tree is
too slow. Without `--buffer`, `cat-file --batch` flushes after each object,
so requests and responses run in lockstep.

Implementation:
- `history.rs::recent_path_hashes(root, retention_days, candidate_paths: &BTreeSet<String>) -> anyhow::Result<HashSet<(String, String)>>`:
  1. `git rev-parse --show-object-format` gives the raw OID width: 20 for
     sha1, 32 for sha256.
  2. `git log --all --since=<N>.days --format=%T` gives the unique root
     trees.
  3. Build the set of directory prefixes of the candidate paths.
  4. One `git cat-file --batch` process walks the raw tree objects from each
     root, memoized on `(prefix, tree oid)`. It descends only into directory
     entries that are candidate prefixes. It records `(path, blob)` for
     modes `100644` and `100755` at candidate paths, and skips `120000`,
     `160000` and names that are not UTF-8.
  5. Deduplicate the pairs. One `git cat-file --batch -Z --filters` process
     reads `<blob> <path>` NUL-framed, so each blob gets its path's
     attributes.
  6. Hash each body with `hasher::content_hash` and insert `(path, hash)`.
- `discover_content_gc` groups candidates per root and calls
  `recent_path_hashes` once per root with that root's candidate paths. A
  candidate is protected only when `(file_path, content_hash)` is in the
  set. An `Err` retains all of that root's candidates and logs a warning.
  `recent_content_hashes_in_git_history` is deleted.

Planned verification:
`cargo nextest run -p gobby-code -E 'test(history)'`, then
`GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test cargo nextest run -p gobby-code -E 'test(content_gc)'`,
then `cargo clippy -p gobby-code` and `cargo fmt -p gobby-code -- --check`.

**Acceptance:**

- 3.1.1 - A blob committed at two paths protects only the version at the
  path where history holds it. test:
  `crates/gcode/src/commands/status/content_gc/history/tests.rs::duplicate_blob_protects_only_its_history_path`.
- 3.1.2 - After a rename inside the window, the old path's content stays
  protected. Content that history holds only at the new path does not
  protect the old path. test:
  `crates/gcode/src/commands/status/content_gc/history/tests.rs::rename_protects_each_path_by_its_own_history`.
- 3.1.3 - Paths with spaces and newlines round-trip. test:
  `crates/gcode/src/commands/status/content_gc/history/tests.rs::unusual_paths_round_trip`.
- 3.1.4 - A per-path `.gitattributes` filter produces the same hash the
  indexer computes for that path's checkout. test:
  `crates/gcode/src/commands/status/content_gc/history/tests.rs::filtered_content_hashes_per_path`.
- 3.1.5 - When git history cannot be read, discovery retains every candidate
  of that root. test:
  `crates/gcode/src/commands/status/content_gc/tests.rs::history_failure_retains_root_candidates`.
- 3.1.6 - A sha256 repository is walked correctly. test:
  `crates/gcode/src/commands/status/content_gc/history/tests.rs::sha256_repository_tree_walk`.

### 3.2 Current-config eligibility for history protection [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/index/walker/classification.rs::passes_path_filters`
- `crates/gcode/src/index/walker.rs::*` — scope-reason: re-export `HistoryPathEligibility` as `pub(crate)`
- `crates/gcode/src/index/indexer/util.rs::effective_excludes`
- `crates/gcode/src/index/indexer.rs::*` — scope-reason: re-export `effective_excludes` as `pub(crate)`
- `crates/gcode/src/commands/status/content_gc.rs::discover_content_gc`
- `crates/gcode/src/commands/status/prune.rs::discover_project_scoped_records`
- `crates/gcode/src/commands/status/content_gc/tests.rs::*` — scope-reason: every `discover_content_gc` call takes `&test_context()`; add the eligibility discovery tests
- `crates/gcode/src/index/walker/tests/classification.rs::*` — scope-reason: add `HistoryPathEligibility` unit tests

Consumers unchanged:
- `crates/gcode/src/index/indexer/pipeline.rs` — no-edit-reason: live discovery keeps its own options.
- `crates/gcode/src/index/walker/hidden.rs` — no-edit-reason: `HiddenPathContext` and `is_hidden_path` are already `pub(super)` to `walker`.
- `crates/gcode/src/index/indexer/freshness_probe.rs` — no-edit-reason: calls `passes_path_filters` and `effective_excludes` unchanged; only their visibility widens.
- `crates/gcode/src/index/indexer/overlay.rs` — no-edit-reason: calls `effective_excludes` unchanged.
- `crates/gcode/src/index/indexer/tests/explicit_routing.rs` — no-edit-reason: exercises `effective_excludes` through indexing unchanged.

**Research context:** see As-Is Facts (classifier). The daemon runs prune per
project root (`--project <root>`), so `prune.rs` has the resolved `Context`.
The `content_gc` tests already build `test_context()`.

Implementation:
- `classification.rs` adds `pub(crate) struct HistoryPathEligibility`,
  holding the root, the effective excludes, `respect_gitignore` and the
  walker's hidden-path allowlist context. It is built by
  `HistoryPathEligibility::new(root, &effective_excludes(&ctx.indexing.extra_excludes), ctx.indexing.respect_gitignore)`.
  `is_eligible(rel)` is true when all of these hold:
  - `passes_path_filters` holds;
  - the path is allowlisted or not `is_hidden_path`;
  - if the file exists now and `respect_gitignore` is set,
    `explicit_path_visible` holds.

  A missing path is never gitignore-excluded. Content checks (generated
  bundle, size) are skipped.
- `effective_excludes` becomes `pub(crate)`, re-exported from `indexer`.
- `discover_content_gc(ctx: &Context, retention_days)` takes the project from
  `ctx.project_id`. An ineligible candidate is collected without a history
  lookup. History (3.1) runs only for eligible candidates.
- `prune.rs::discover_project_scoped_records` passes `&ctx`.
- Worktrees: each project root follows these rules with its own config. A
  root whose checkout is gone fails its git calls and retains its
  candidates. Stale-project prune owns removing that root.

Planned verification:
`cargo nextest run -p gobby-code -E 'test(classification)'`, then
`GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test cargo nextest run -p gobby-code -E 'test(content_gc) | test(prune)'`,
then `cargo clippy -p gobby-code` and `cargo fmt -p gobby-code -- --check`.

**Acceptance:**

- 3.2.1 - A candidate under an `extra_excludes` pattern is collected even
  though history holds it at that path. test:
  `crates/gcode/src/commands/status/content_gc/tests.rs::excluded_path_is_collected_despite_history`.
- 3.2.2 - A hidden path outside the allowlist is ineligible, and an
  allowlisted hidden path is eligible. test:
  `crates/gcode/src/index/walker/tests/classification.rs::history_eligibility_applies_hidden_allowlist`.
- 3.2.3 - A deleted path that passes the lexical filters stays eligible and
  is protected by its history. test:
  `crates/gcode/src/commands/status/content_gc/tests.rs::missing_path_keeps_history_protection`.
- 3.2.4 - An existing gitignored file is ineligible when `respect_gitignore`
  is set, and eligible when it is not. test:
  `crates/gcode/src/index/walker/tests/classification.rs::history_eligibility_respects_gitignore_for_existing_files`.

## P4: Documentation
`kind: framing`

### 4.1 Guides for reconcile and path-aware retention [category: docs] (depends: P2, P3)
`kind: deliverable`

Targets:
- `docs/guides/code-index.md`
- `docs/guides/gcode-user-guide.md`
- `docs/guides/gcode-graph-core.md`
- `docs/guides/gcode-development-guide.md`
- `crates/gcode/README.md`

**Research context:** stale text sits at:
- `code-index.md:192`;
- `gcode-user-guide.md:445-468` (prune paragraph and projection cleanup)
  and 546-560;
- `gcode-graph-core.md:127-137`, which also claims prune sweeps vector
  orphans;
- `gcode-development-guide.md:323-329`;
- `crates/gcode/README.md:165`.

Implementation:
- Each `vector cleanup-orphans` mention becomes `vector reconcile`: a
  read-only inventory by default, and `--apply --expect-digest --receipt`
  under the maintenance lease through the reviewed procedure.
- Remove the claim that `gcode prune` composes projection cleanup.
- The prune paragraph says history protects a version only at its own path,
  and only while the path passes the current excludes, hidden and gitignore
  rules.

Planned verification: `uv run gobby plans validate .gobby/plans/code-index-vector-reconciliation.md -p /Users/josh/Projects/gobby`
and a read-through against the shipped `gcode vector reconcile --help`.

**Acceptance:**

- 4.1.1 - The user guide documents the dry-run classes, the digest, and the
  apply flags with their lease and receipt rules. behavior:
  "vector reconcile" in `docs/guides/gcode-user-guide.md`.
- 4.1.2 - No guide or README names `vector cleanup-orphans`, and the
  graph-core guide no longer says prune sweeps vector orphans. behavior:
  "vector reconcile" in `docs/guides/gcode-graph-core.md`.
- 4.1.3 - The prune text states path-keyed, eligibility-gated history
  retention. behavior: "its own path" in `docs/guides/gcode-user-guide.md`.

## D1 Hourly reconcile apply (depends: 2.2)
`kind: deferred`

The daemon's hourly prune (`src/gobby/code_index/prune.py`, through
`gcode_gateway.py`) runs `vector reconcile` and applies with the digest it
just observed. This waits until the Live Maintenance Procedure has run once
and its receipt has been reviewed, because the first apply removes a
historical backlog that needs human review. Automating it also needs a
decision on receipt storage for unattended runs.

```yaml
deferral:
  task_ref: "created-at-expansion"
  reason: "Unattended apply is gated on the first reviewed live apply and its receipt; receipt storage for unattended runs needs its own decision."
  owner: "orchestrator"
  original_acceptance_items:
    - D1.1
```

- D1.1 - The hourly prune runs reconcile for each project. It applies only
  orphan deletion and flag reset, with a receipt under the daemon's private
  state directory, and reports counts in the prune result.

## Live Maintenance Procedure
`kind: framing`

An operator named by the Orchestrator runs the procedure from the main
checkout. Josh approves step 3. No PostgreSQL purge, GC run or collection
drop is part of it.

0. **Before promoting the binary that carries P3.** In a private directory
   outside the repository, run:

   ```bash
   echo n | <candidate gcode> --project /Users/josh/Projects/gobby prune --retention-days <N> 2> <dir>/gc-preview-candidate.txt
   ```

   `<N>` is the daemon's `content_retention_days` (`prune.py:264`). Then run
   the same command with the installed `~/.gobby/bin/gcode`. Discovery lists
   every candidate on stderr, and `n` declines, so nothing is deleted. Send
   both counts, and the candidates only the new rules collect, to the
   Orchestrator. Promotion waits for its go. This is the evidence for the
   1,314 versus 1,451 hypothesis.
1. **After 2.1 and 2.2 are promoted, in a quiet window**, run the dry-run
   twice and require equal digests:

   ```bash
   gcode --project <root> vector reconcile --format json > <dir>/reconcile-dry-1.json
   gcode --project <root> vector reconcile --format json > <dir>/reconcile-dry-2.json
   ```

   Run once per project ID; checkouts that share a project ID share one
   collection. Different digests mean live drift: stop, wait for the indexer
   to settle, and repeat.
2. Send the per-class counts, the digest and the file paths to the
   Orchestrator. The Orchestrator reviews them against the hypotheses table
   and asks Josh to approve that digest.
3. In a window the Orchestrator announces, run:

   ```bash
   gcode --project <root> vector reconcile --apply --expect-digest <digest> --receipt <dir>/reconcile-apply.json --format json
   ```

   Busy (the hourly prune holds the lease) and digest mismatch both leave
   everything untouched. On busy, rerun later. On a mismatch, return to
   step 1.
4. **Verify.** Run a dry-run and confirm zero orphans and zero synced-missing
   versions, except points recreated by a detached writer, which are listed
   and handled by the next pass. Once the sync worker drains, `pending`
   returns to its pre-apply level. Send the receipt path and the
   verification output to the Orchestrator.

## Rollout
`kind: framing`

1. Leaves land through the lane review path. Each crate change becomes
   live only after a rebuild and promotion through `promote_workspace_binary_set`.
   The 1.2 Python change becomes live only after a daemon restart, which
   happens only with the Orchestrator's global announcement.
2. 1.1 and 1.2 are safe in either activation order:
   - With the old gcode and the new worker, the old native command still
     marks completion.
   - With the new gcode and the old worker, the S2 race persists until the
     restart, and nothing breaks.
3. Run Live Maintenance Procedure step 0 before promoting the gcode that
   carries P3. P3 changes what the hourly prune collects.
4. Run the Live Maintenance Procedure after P2 is promoted. D1 waits for its
   receipt.
5. Route the graph-side analog (Decision Record 12) to the Orchestrator when
   the plan is sent for approval.

## V1 Plan Changelog
`kind: verification`

- 2026-10-05 14:52 CDT: First draft by the Lane 7 Plan Writer gobby#15429 on
  #22958. Design choices:
  - The missing-vector repair is hub-wide per version, following the #19743
    shared-hub topology.
  - The dry-run evidence is JSON on stdout.
  - The completion fix is split into the Rust compare-and-set (1.1) and the
    Python worker change (1.2).

## V2: Verification
`kind: verification`

Each leaf runs its planned verification after its final edit. After the last
leaf lands:

```bash
cargo nextest run -p gobby-code
GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test cargo nextest run -p gobby-code -E 'test(serial_db) | test(mark_vectors_synced) | test(dirty_vector_sync) | test(history) | test(reconcile)'
cargo clippy -p gobby-code && cargo fmt -p gobby-code -- --check
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_sync_worker.py tests/code_index/test_sync_worker_breaker.py tests/test_runner_code_index_shutdown.py tests/code_index/test_code_index_storage.py tests/skills/test_reference_library.py -q
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
uv run gobby plans validate .gobby/plans/code-index-vector-reconciliation.md -p /Users/josh/Projects/gobby
```
