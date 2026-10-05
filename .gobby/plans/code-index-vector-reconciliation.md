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
- It stops new drift on both projection flags. gcode becomes the only owner
  of vector-sync completion, through a compare-and-set that mirrors the graph
  one. A failed completion of either flag also dirties the version it
  attempted (1.1). The graph CLI honors its existing compare-and-set (1.4). The
  Python worker stops marking either flag after a native call, and sends
  every graph-language file to gcode for graph-fact eligibility (1.2). A
  re-parse of an existing content version marks a projection pending when
  that projection's stored input changes (1.3).
- `gcode vector reconcile` replaces `gcode vector cleanup-orphans`. A
  read-only dry-run inventories stale points and missing vectors for one
  project collection (2.1). `--apply`, run under the maintenance lease,
  deletes only point IDs that PostgreSQL no longer has, resets the sync flag
  of synced versions whose points are missing, and writes a receipt (2.2).
- Content GC protects a version only when its exact path and content hash
  appear in recent git history, and only when that path is still eligible
  under the project's current indexing config (3.1, 3.2).

Ownership:
- The `gobby-code` crate implements 1.1, 1.3, 1.4, P2 and P3.
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
   Reconcile never embeds. The reset makes every affected version repairable
   through existing scheduling. The daemon worker selects only versions its
   own machine's file states reference (`_storage/files.py:198-209`):
   - a version this machine references drains through the local worker;
   - a version only another machine references drains through that
     machine's worker;
   - a retained version that no machine references stays pending until a
     machine adopts it, and adoption keeps the false flag.
4. **Completion ownership.** The gcode completion compare-and-sets own
   `vectors_synced = true` and `graph_synced = true` after a projection
   write. The Python worker stops marking either flag after a native call
   (`sync_worker.py:583` and `:677`). Its attempt marks (`:497` and `:599`)
   stay, because they only clear the flag; the native command marks again
   and captures the attempt it completes.
5. **Re-parse reset.** Writing facts for an existing version reads that
   version's stored facts before and after the write, and resets each flag
   in the same transaction when its own projection input changed:
   - vectors: the `BTreeMap<symbol id, vector_text_for_symbol>`;
   - graph: the imports, calls and inheritance rows, and per symbol the
     fields the graph writes (`id`, `name`, `qualified_name`, `kind`,
     `language`, `line_start`, `line_end`; `mutation.rs:745-757`).

   A full `Symbol` comparison is never used, because `updated_at` changes on
   every upsert.
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
   for the next pass. A stale writer can also overwrite a version's points
   or graph facts with older input after another attempt synced that
   version (S9). Its completion then fails the compare-and-set, and the
   failure dirties the attempted version as well as this machine's current
   row (1.1), so whichever machine references that version re-syncs it. A
   stale write whose process exits before its completion runs is not
   detected. No fencing is added.
9. **History protection.** It keys on `(path, content_hash)`, gathered by one
   `cat-file` tree walk pruned to candidate paths. The root trees come from
   every reachable commit dated inside the window, including ancestors of
   older-dated tips. When history cannot be read, every eligible candidate
   of that root is retained.
10. **Eligibility.** Root availability comes first. A root that is not an
    existing directory retains every candidate, and stale-project prune owns
    removing it. Under an existing root, a candidate whose path is
    ineligible under current config is collected whatever its history, even
    when history cannot be read. Eligibility reads only config and the
    working tree. A missing path is never treated as gitignore-excluded.
11. **Automation.** An hourly automated apply is deferred (D1) until the live
    procedure has run once.
12. **Graph flag parity.** S6 and S7 are the same flag-race class as S1 and
    S2 on the graph side. Fixing only the vector flag would leave the class
    half fixed, so `graph_synced` gets the same rules: the graph CLI reports
    a failed compare-and-set as a superseded skip (1.4), the worker leaves
    completion to gcode (1.2), and a re-parse resets the flag when the graph
    input changes (1.3). `graph rebuild` keeps ignoring the result. It runs
    under the exclusive project lock (`lifecycle.rs:446`), and a failed
    compare-and-set there leaves the row pending, which is safe.
13. **Graph eligibility and the no-graph shortcut.** gcode owns graph-fact
    eligibility. `_file_needs_graph_sync` keeps only its language check, so
    every file in a graph language goes to the native command. That command
    applies `has_no_graph_facts` over imports, symbols, calls and
    inheritance. For a file with none of them, it clears the path's stale
    graph through `sync_no_fact_file`. This fixes S8. Files in other
    languages keep the Python shortcut, which saves one gcode call per
    non-code file. The shortcut's mark becomes a compare-and-set on the
    snapshot's `language`, the only field the decision now reads. A
    same-hash re-parse rewrites `language` (`index/api.rs:274-279`), so a
    concurrent re-parse that changes it leaves the row pending for the next
    scan.

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

  A new version is inserted with false and NULL flags. On conflict,
  `upsert_file` rewrites `language`, `symbol_count`, `byte_size` and the
  timestamps, and keeps both flags (`index/api.rs:274-279`, excerpt_hash
  `a739e9d5d655318986969b3447de1b2124ed2fe4379c0840d7af094e739cb14f`). A `--full`
  re-parse skips adoption (`index/indexer/pipeline.rs:152-167`).
- **Completion marks.** `mark_vector_sync_attempted` and
  `mark_vectors_synced` (`db/queries.rs:235-275`) match on project and path
  through this machine's file state. Neither checks a hash or an attempt.
  The graph pair is a compare-and-set: `mark_graph_sync_attempted` returns
  `GraphSyncAttempt`, and on failure `mark_graph_synced` dirties only this
  machine's current row for the path (`db/queries.rs:112-186`, excerpt_hash
  `9ec8c082913683e5bdf5404fbfac9196985dfe7d67d80529d14cb0f2e5ecf318`).
- **Callers of the marks.**
  - `projection/sync.rs::VectorProjectionState::sync_file` (646-661) runs
    inside a bounded worker. The caller abandons a stalled worker after 300
    seconds and releases its lock (280-342).
  - CLI `vector sync-file` (`commands/vector.rs:43-85`) holds the file lock.
  - The Python worker `_sync_file` reads `current`, then
    `mark_vector_sync_attempted(current.id)` (:497), then runs gcode with
    `--allow-missing-indexed-file`, then
    `mark_vectors_synced(current.id, current.content_hash)` (:583).
  - `_storage/files.py::mark_vectors_synced` is the only Python vector mark.
- **Graph completion.**
  - `projection/sync.rs::sync_graph_file` honors the graph compare-and-set
    and reports a skip when it fails.
  - The graph CLI's `sync_file_graph` discards the `mark_graph_synced`
    result on both of its paths: the no-graph-facts path
    (`commands/graph/lifecycle.rs:239-245`, excerpt_hash
    `f7a5528f16f1123f22cc2429ab66b2f710302ac3b6f64fe7687d97882f20983b`) and
    the projection path (`:264-270`, excerpt_hash
    `ad858633c348c40945d764f44ec06f52472f6021b7cc579c7f6a2cfc47062b92`). It
    returns `Synced` or `SkippedNoGraphFacts` either way.
  - The Python worker marks `current.id` graph-synced after any
    non-degraded native result (`sync_worker.py:672-681`, excerpt_hash
    `2c69b34d56ed9d10c8fe1291adf6d503b6d68c32a5e4f5939187259e9e5e8aca`). It
    also marks without a native call when `_file_needs_graph_sync` is false
    (`:587-594`, excerpt_hash
    `55ef450cedf3ecea5e708d7a2373d68829b11039352d62ebd5cb5c0ed0182382`): no
    symbols, or a language outside `_GRAPH_SYNC_LANGUAGES`
    (`sync_worker.py:79-80`, excerpt_hash
    `0de87c61266a9fed498704611dbfd6c38b41dfe63110466e1aafdf13df06679d`).
  - Native eligibility is `has_no_graph_facts`, which requires empty
    imports, definitions, calls and inheritance (`lifecycle.rs:203-210`,
    excerpt_hash
    `704272dbbf04d0dd7595e2924248a8a140fd72a6920f4deea1ee54096bddbfd3`). For
    a file with no facts, `sync_no_fact_file`
    (`graph/code_graph/write.rs:171-178`) deletes the path's stale graph
    and its bare file node, and writes nothing.
  - `_storage/files.py::mark_graph_synced(file_id, content_hash)` matches
    the version row only and stamps a Python-side attempt time.
  - `db/queries.rs::dirty_graph_sync_for_file` joins any machine's file
    state, so it skips versions no state references. Inheritance promotion
    in `index/indexer/local_imports.rs` is its only production caller.
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
- **S6.** The S2 race on the graph flag. The graph CLI's discarded
  compare-and-set result reaches the worker as success, and the worker then
  marks `current.id` graph-synced even when the native command projected a
  newer version.
- **S7.** A same-version re-parse that changes imports, calls, inheritance
  or projected symbol fields keeps `graph_synced = true`, so the graph keeps
  the old facts.
- **S8.** The worker's eligibility differs from native's. A graph-language
  file with imports, calls or inheritance but no symbols, such as an
  `__init__.py` that only imports, is marked graph-synced without its edges
  being projected. When a file's new version has no symbols, the path's old
  graph nodes also stay, because the native cleanup never runs.
- **S9.** A failed completion dirties only this machine's current row.
  Suppose machine A's stalled write for version H resumes after machine B
  re-parsed H with the same symbol IDs and changed vector text or calls,
  and re-synced it. A's write restores the older input, but its failed
  completion leaves H marked synced. Symbol IDs carry the content hash
  (`models.rs:159-170`, excerpt_hash
  `62753da9201589d6fc37255061bd3e5e9929194fdd7f19963bcc02a61103bc2a`).
  Vector text covers the docstring (`vector/code_symbols/embedding.rs:390-442`,
  excerpt_hash
  `e35f1f614084f2b200d45abb28ea29f30e7c5c62663ace324bda3c823582b643`).
  Points are upserted by symbol ID (`vector/code_symbols/lifecycle.rs:302-360`,
  excerpt_hash
  `ad8e202c6a1b23e14616db0b590d8449588279e01cf83ac42d007aea3b6926dd`). The
  graph writer replaces a path's facts for one content hash
  (`graph/code_graph/write.rs:67-178`, excerpt_hash
  `0fcc0a04dfd52008e834b7ed00c857365a15399bffecb89d87db34b16bdb0a67`).
- **S10.** `recent_content_hashes_in_git_history` passes `--since` to
  `rev-list --all` (`content_gc.rs:374-415`, excerpt_hash
  `8299951a0293592be2985bc36cdb8cd20168e9f6ae5bc73d523da6ca4b08f535`). That
  option stops traversal at an older-dated commit, so a recent ancestor of
  an older-dated tip is silently left out of the protection set.

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
  - `commands/graph/lifecycle.rs` (625), `index/indexer/file.rs` (288);
  - `src/gobby/code_index/sync_worker.py` (728).

  `models.rs` (951 lines) is already above that trigger and below the
  1,000-line ceiling. 1.3 changes only three `derive` lines there, with no
  net line change.

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

**Goal:** completion of both projection flags is a compare-and-set owned by
gcode, and a re-parse can no longer leave changed projection input behind a
true flag.

### 1.1 Vector completion compare-and-set and attempted-version recovery [category: code]
`kind: deliverable`

Targets:
- `crates/gcode/src/db/queries.rs::mark_vector_sync_attempted`
- `crates/gcode/src/db/queries.rs::mark_vectors_synced`
- `crates/gcode/src/db/queries.rs::mark_graph_synced`
- `crates/gcode/src/projection/sync.rs::VectorProjectionState`
- `crates/gcode/src/commands/vector.rs::sync_file`
- `crates/gcode/src/index/api_tests.rs::*` — scope-reason: the attempt and completion calls at 111-133 move to the new signatures
- `crates/gcode/src/db/queries_cas_tests.rs::*` — scope-reason: add vector compare-and-set tests beside the graph ones
- `crates/gcode/src/projection/sync/tests.rs::*` — scope-reason: add a `serial_db` module with the superseded-completion test
- `crates/gcode/src/vector/code_symbols.rs::*` — scope-reason: the `cfg(test)` `mod tests` declaration becomes `pub(crate)` so the projection and reconcile tests reach its HTTP helpers
- `crates/gcode/src/vector/code_symbols/tests.rs::*` — scope-reason: `accept_with_timeout` and `read_http_request` become `pub(crate)` for the projection and reconcile tests

Consumers unchanged:
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: it already passes `--allow-missing-indexed-file`, and the skip payload keys are unchanged.
- `crates/gcode/src/vector/code_symbols/lifecycle.rs` — no-edit-reason: upsert batching is unchanged.

**Research context:** see As-Is Facts (completion marks, callers, locks). The
graph compare-and-set in `db/queries.rs:13-186` and its tests in
`db/queries_cas_tests.rs` are the pattern. The project-level
`mark_project_vector_sync_attempted` and `mark_project_vectors_synced` serve
`vector rebuild` under the exclusive project lock and stay as they are.
`mark_graph_synced` is called by `projection/sync.rs::sync_graph_file`, the
graph CLI's `sync_file_graph` (1.4) and `rebuild_project_graph`. Rebuild runs
under the exclusive project lock, so its completions do not fail in
practice, and a failure there now also dirties the attempted version, which
is safe.

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
  still references. When nothing matches, a second CTE in the same statement
  sets `vectors_synced = false` and `vector_sync_attempted_at = NULL` on two
  rows, and the call returns false:
  - this machine's current row for the path, as `mark_graph_synced` does
    today;
  - the attempted version `(project, path, content_hash)`, with no
    file-state join, when that row still exists. A stale write may have
    overwritten its points after another attempt synced it (S9), whichever
    machine references it, including none.
- `mark_graph_synced` adds the same attempted-version row to its failure
  CTE, for `graph_synced` and `graph_sync_attempted_at`. Both statements
  keep this SQL local. 1.1 does not call 1.3's `dirty_version_sync`, which
  would invert the 1.3 dependency.
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
`GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test cargo nextest run -p gobby-code -E 'test(mark_vectors_synced) | test(mark_vector_sync) | test(mark_graph_synced) | test(serial_db) | test(api_tests)'`,
then `cargo clippy -p gobby-code` and `cargo fmt -p gobby-code -- --check`.

**Granularity:** four production files and six acceptance items, one
outcome: a completion either matches its attempt or leaves every version its
write could have touched pending. The graph change is that failure rule in
the sibling statement of the same file and test file. Splitting it out would
put two leaves on one query and one test module.

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
- 1.1.5 - A failed vector completion dirties the attempted version as well
  as this machine's current row. The fixture is the S9 case: this machine's
  state has moved from H to K, and H is `vectors_synced = true` under a
  later attempt, as after another machine re-synced changed docstring text.
  It runs twice, once with H referenced by another machine's state and once
  with no state referencing H. Each time, completion with the old H attempt
  returns false and leaves both H and K false with a NULL attempt. test:
  `crates/gcode/src/db/queries_cas_tests.rs::mark_vectors_synced_cas_failure_dirties_attempted_version`.
- 1.1.6 - The same holds for `mark_graph_synced`, with H re-synced on the
  graph flag after a calls and imports change, in the same two runs. test:
  `crates/gcode/src/db/queries_cas_tests.rs::mark_graph_synced_cas_failure_dirties_attempted_version`.

### 1.2 Sync worker leaves projection completion to gcode [category: code] (depends: 1.1, 1.4)
`kind: deliverable`

Targets:
- `src/gobby/code_index/sync_worker.py::_sync_file`
- `src/gobby/code_index/sync_worker.py::_file_needs_graph_sync`
- `src/gobby/code_index/_storage/files.py::CodeIndexFileStorageMixin`
- `tests/code_index/test_sync_worker.py::*` — scope-reason: drop `mark_vectors_synced` assertions and fakes, move `mark_graph_synced` assertions to the shortcut-only snapshot call, add the completion and import-only tests
- `tests/code_index/test_sync_worker_breaker.py::*` — scope-reason: drop `mark_vectors_synced` fakes and assertions; graph-mark counts drop to shortcut calls only
- `tests/test_runner_code_index_shutdown.py::*` — scope-reason: its storage fake defines `mark_vectors_synced`
- `tests/code_index/test_code_index_storage.py::*` — scope-reason: the stale-hash test at 1689 calls the new `mark_graph_synced(file)` signature; add the shortcut snapshot test

Consumers unchanged:
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: `vector_sync_file` already returns the native payload; 1.1 owns the native skip payload.
- `src/gobby/code_index/storage.py` — no-edit-reason: composes the mixin; no caller of the removed method remains after 1.2.

**Research context:** `_sync_file` (`sync_worker.py:466-689`) treats a
native result that passes `_require_projection_success` as terminal, then
marks `current.id` vector-synced at :583 and graph-synced at :677. See As-Is
Facts (graph completion) for the no-graph shortcut at :587-594.
`_storage/files.py:211` (`mark_vectors_synced`) and `:234`
(`mark_graph_synced`) are the only definitions, and the test references sit
in the four test files above.

Implementation:
- Delete the `storage.mark_vectors_synced` call at :583 and the
  `storage.mark_graph_synced` call at :677. Keep breaker bookkeeping and
  `did_work = True`.
- Delete `CodeIndexFileStorageMixin.mark_vectors_synced` and every test
  reference and fake.
- `CodeIndexFileStorageMixin.mark_graph_synced(file: IndexedFile) -> bool`
  serves only the shortcut (Decision Record 13). It runs
  `UPDATE code_indexed_files SET graph_synced = TRUE, graph_sync_attempted_at = %s WHERE id = %s AND content_hash = %s AND language = %s`
  with the snapshot's values. The shortcut at :592 passes `current`.
- `_file_needs_graph_sync(file)` returns
  `file.language.lower() in _GRAPH_SYNC_LANGUAGES`, without the
  `symbol_count > 0` check (Decision Record 13, S8).
- Keep `mark_vector_sync_attempted` (:497), `mark_graph_sync_attempted`
  (:599), `requeue_vector_sync` and `requeue_graph_sync`.

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
- 1.2.3 - A successful native graph result leaves `graph_synced` as gcode
  wrote it. The worker calls no graph mark after a native call. test:
  `tests/code_index/test_sync_worker.py::test_graph_sync_leaves_completion_to_native_cas`.
- 1.2.4 - A native graph `sync_superseded` skip raises no error and leaves
  the file pending. test:
  `tests/code_index/test_sync_worker.py::test_superseded_graph_skip_keeps_file_pending`.
- 1.2.5 - The shortcut mark returns false and leaves `graph_synced = false`
  when the row's `language` no longer matches the snapshot, and marks the
  row when it matches. test:
  `tests/code_index/test_code_index_storage.py::test_no_graph_shortcut_mark_rejects_changed_snapshot`.
- 1.2.6 - A Python `__init__.py` with imports and no symbols
  (`symbol_count = 0`) goes to the native graph sync, and the worker makes
  no shortcut mark. A file outside `_GRAPH_SYNC_LANGUAGES` still takes the
  shortcut. test:
  `tests/code_index/test_sync_worker.py::test_import_only_init_file_delegates_graph_sync_to_native`.

### 1.3 Re-parse projection reset [category: code] (depends: 1.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/index/indexer/file.rs::write_parsed_file_facts`
- `crates/gcode/src/index/indexer/sink.rs::CodeFactSink`
- `crates/gcode/src/index/indexer/sink.rs::PostgresCodeFactSink`
- `crates/gcode/src/db/queries.rs::read_graph_file_facts`
- `crates/gcode/src/models.rs::ImportRelation`
- `crates/gcode/src/models.rs::CallRelation`
- `crates/gcode/src/models.rs::InheritanceRelation`
- `crates/gcode/src/vector/code_symbols.rs::*` — scope-reason: re-export `vector_text_for_symbol` as `pub(crate)` outside `cfg(test)`
- `crates/gcode/src/db/queries_cas_tests.rs::*` — scope-reason: add the version-dirty query test
- `crates/gcode/src/index/indexer/tests/facts.rs::*` — scope-reason: `RecordingCodeFactSink` implements the two new trait methods; add the reset unit tests
- `crates/gcode/src/index/indexer/tests/serial_db.rs::*` — scope-reason: add the full re-index test
- `docs/guides/gcode-development-guide.md`

Consumers unchanged:
- `crates/gcode/src/index/indexer/pipeline.rs` — no-edit-reason: adoption and `--full` routing are unchanged.
- `crates/gcode/src/vector/code_symbols/embedding.rs` — no-edit-reason: `vector_text_for_symbol` is already `pub`.
- `crates/gcode/src/index/indexer/local_imports.rs` — no-edit-reason: inheritance promotion keeps `dirty_graph_sync_for_file` and its file-state join.
- `crates/gcode/src/graph/code_graph/write/mutation.rs` — no-edit-reason: the symbol fields the graph writes define the graph comparison key and are read only.

**Research context:** see As-Is Facts (fact write order, graph completion).
`read_graph_file_facts` (`db/queries.rs:82-110`) resolves this machine's
current hash, then calls the private version-keyed readers
`read_imports_for_file`, `read_symbols_for_file`, `read_calls_for_file` and
`read_inheritance_for_file`. Each reader has an `ORDER BY`. `db/mod.rs`
re-exports `queries::*`. Vector text (`embedding.rs:401-432`) covers name,
qualified name, kind, language, path, range, signature, docstring and
summary. Upserts preserve stored summaries. `ImportRelation`,
`CallRelation` and `InheritanceRelation` derive only `Debug, Clone`; their
enum fields `CallTargetKind` and `HeritageKind` already derive
`PartialEq, Eq`.

Implementation:
- New `pub(crate) fn read_version_graph_facts(conn, project_id, file_path, content_hash) -> anyhow::Result<GraphFileFacts>`
  holds the four reader calls. `read_graph_file_facts` resolves the hash
  and delegates to it. The four readers stay private.
- `ImportRelation`, `CallRelation` and `InheritanceRelation` derive
  `PartialEq, Eq` on their existing `derive` lines.
- New `pub(crate) fn dirty_version_sync(conn, project_id, file_path, content_hash, vectors: bool, graph: bool) -> anyhow::Result<bool>`
  updates that version row with no file-state join. For each flag passed
  true, it sets the flag false and its attempt NULL. It leaves the other
  flag's columns as they are.
- `CodeFactSink` gains `read_version_facts(project_id, file_path, content_hash) -> GraphFileFacts`
  and `dirty_version_sync(project_id, file_path, content_hash, vectors, graph)`.
  `PostgresCodeFactSink` delegates both to `db`.
- `write_parsed_file_facts` reads `before` first.
  - When all four `before` lists are empty, it skips the re-read. The
    vector flag is dirtied if the parse produced symbols. The graph flag is
    dirtied if the parse produced any symbol, import, call or inheritance
    row.
  - Otherwise it re-reads `after` once the fact writes finish, and compares:
    - vectors: `BTreeMap<id, vector_text_for_symbol>` of the definitions;
    - graph: the imports, calls and inheritance lists, and the per-symbol
      tuple `(id, name, qualified_name, kind, language, line_start, line_end)`.
  - It calls `dirty_version_sync` once with the flags whose input differs.
    The reset joins the sink's transaction. `FileIndexCounts` is unchanged.
  - Cost: four indexed reads per indexed file, plus four more when the
    version already has facts. An `ORDER BY` tie can reorder rows and
    dirty the graph flag spuriously. That over-reset is safe and costs one
    graph re-sync.
- In `gcode-development-guide.md` item 5, a re-parse of an existing version
  keeps its sync flags unless that projection's stored input changed. When
  it did, that flag resets to pending.

Planned verification:
`GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test cargo nextest run -p gobby-code -E 'test(facts) | test(serial_db) | test(dirty_version_sync)'`,
then `cargo clippy -p gobby-code` and `cargo fmt -p gobby-code -- --check`.

**Granularity:** eight acceptance items, one outcome: a re-parse resets
exactly the flags whose projection input changed. The items are the cases of
one read, compare and dirty step in one transaction: both flags, neither,
empty, all facts dropped, `--full`, the helper's scope, vectors only and
graph only. Splitting vectors from graph would put two leaves on the same
function, transaction and test file.

**Acceptance:**

- 1.3.1 - Re-writing a version whose symbol set changes dirties both
  flags in one call. test:
  `crates/gcode/src/index/indexer/tests/facts.rs::reparse_with_changed_symbol_set_dirties_both_flags`.
- 1.3.2 - Re-writing a version with identical stored facts and identical
  vector text leaves both flags alone. test:
  `crates/gcode/src/index/indexer/tests/facts.rs::reparse_with_identical_facts_keeps_both_flags`.
- 1.3.3 - An empty stored version with an empty parse does not dirty. test:
  `crates/gcode/src/index/indexer/tests/facts.rs::empty_reparse_of_empty_version_keeps_flags`.
- 1.3.4 - A non-empty stored version with an empty parse dirties both
  flags. test:
  `crates/gcode/src/index/indexer/tests/facts.rs::reparse_that_drops_all_facts_dirties_both_flags`.
- 1.3.5 - A `--full` re-index that changes symbol IDs of a version synced
  on both flags leaves it `vectors_synced = false` and
  `graph_synced = false`, each with a NULL attempt. test:
  `crates/gcode/src/index/indexer/tests/serial_db.rs::full_reindex_with_changed_symbol_ids_marks_projections_pending`.
- 1.3.6 - `dirty_version_sync` changes only the named version and only the
  named flags, even when no file state references the version. test:
  `crates/gcode/src/db/queries_cas_tests.rs::dirty_version_sync_touches_only_named_flags_and_version`.
- 1.3.7 - A re-parse that keeps the symbol ID set but changes a stored
  docstring dirties only the vector flag, with a NULL vector attempt. test:
  `crates/gcode/src/index/indexer/tests/facts.rs::reparse_with_changed_docstring_dirties_vectors_only`.
- 1.3.8 - A re-parse that keeps the symbols but changes a call or an import
  dirties only the graph flag, with a NULL graph attempt. test:
  `crates/gcode/src/index/indexer/tests/facts.rs::reparse_with_changed_calls_dirties_graph_only`.

### 1.4 Graph CLI honors the completion compare-and-set [category: code]
`kind: deliverable`

Targets:
- `crates/gcode/src/commands/graph/lifecycle.rs::GraphFileSyncOutcome`
- `crates/gcode/src/commands/graph/lifecycle.rs::sync_file_graph`
- `crates/gcode/src/commands/graph/lifecycle.rs::sync_file`
- `crates/gcode/src/commands/graph/tests.rs::*` — scope-reason: add the superseded payload test and a `serial_db` module with the completion test

Consumers unchanged:
- `src/gobby/code_index/gcode_gateway.py` — no-edit-reason: `graph_sync_file` returns the native payload and keys on no skip reason.

**Research context:** see As-Is Facts (graph completion).
`projection/sync.rs::sync_graph_file` already honors the compare-and-set
(`:625-633`). 1.1 changes only the failure branch of `mark_graph_synced`,
which that path already reports as a skip. `rebuild_project_graph`
also discards the result (`lifecycle.rs:346` and `:364`) and stays as it
is (Decision Record 12). `sync_file_graph` needs FalkorDB
(`code_graph::require_graph_reads`, `code_graph::sync_file_graph`), so the
completion step becomes a function that PostgreSQL alone can test. The
skip payload shape follows `skipped_no_graph_facts_payload`, and
`no_graph_facts_skip_payload_is_terminal_success_shape` is the test
pattern.

Implementation:
- `GraphFileSyncOutcome` gains `SkippedSuperseded`.
- New `fn complete_graph_sync(conn, project_id, file_path, attempt: &GraphSyncAttempt, outcome: GraphFileSyncOutcome) -> anyhow::Result<GraphFileSyncOutcome>`
  calls `mark_graph_synced` with the attempt. It returns `outcome` on
  success and `SkippedSuperseded` on failure. Both completion sites in
  `sync_file_graph` (no graph facts and projected) call it.
- New `skipped_superseded_payload(ctx, file_path)`: `status: "skipped"`,
  `reason: "sync_superseded"`, `synced_files: 0`, `skipped_files: 1`,
  `degraded: false`, `error: null`, and summary
  `skipped graph sync for <path>: superseded by a newer index`.
- `sync_file` prints it with the file-lock fields and exits 0. Text
  format prints a one-line skip like the other two skips.

Planned verification:
`GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test cargo nextest run -p gobby-code -E 'test(commands::graph)'`,
then `cargo clippy -p gobby-code` and `cargo fmt -p gobby-code -- --check`.

**Acceptance:**

- 1.4.1 - The superseded skip payload is a terminal success shape with
  reason `sync_superseded` and no degradation. test:
  `crates/gcode/src/commands/graph/tests.rs::superseded_skip_payload_is_terminal_success_shape`.
- 1.4.2 - After this machine's state moves to another hash, completing
  the old attempt returns `SkippedSuperseded`, and the new current row is
  `graph_synced = false` with a NULL attempt. Completing a current attempt
  returns the given outcome and marks the row. test:
  `crates/gcode/src/commands/graph/tests.rs::graph_completion_reports_superseded_after_state_moves`.

## P2: Vector Reconcile
`kind: framing`

**Goal:** one command proves the vector projection matches PostgreSQL for a
project. Under the lease and with a receipt, it deletes only what PostgreSQL
disowns. It also makes every version with missing vectors repairable by the
existing sync scheduling (Decision Record 3).

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
  - `pending_current`: flag false and referenced by a state.
  - `pending_retained`: flag false and referenced by none. These wait for
    adoption (Decision Record 3).
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
- 2.1.2 - Versions missing points are split by flag and by state
  reference into the four missing classes. test:
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
  4. Check any existing receipt with `read_existing`. One for the same
     project, collection and digest is overwritten by this run. One for any
     other identity, or an unreadable file, is refused with no mutation.
  5. Write the initial receipt, a serde `ReconcileReceipt` struct in
     `reconcile.rs`: digest, project, collection, the planned
     `version_ids` and orphan IDs per class, `versions_reset: []`,
     `deleted: []`, `complete: false`. Planned and confirmed lists stay
     separate. A write failure exits nonzero with no mutation.
  6. In one transaction, run `UPDATE code_indexed_files SET vectors_synced = false, vector_sync_attempted_at = NULL WHERE id = ANY($1) AND vectors_synced RETURNING id`
     for the synced-missing version IDs, then commit. The returned IDs
     become `versions_reset` only after the commit succeeds, and then the
     receipt is rewritten. A commit error exits nonzero, reports the reset
     as unconfirmed with the receipt path, and sends no delete. The receipt
     keeps `versions_reset: []`.
  7. Delete orphan IDs with `delete_symbol_vectors` in batches of 256. Append
     each batch to `deleted` and rewrite the receipt after it.
  8. On success, set `complete: true` and exit 0.
- **Receipt persistence.** The apply sequence writes every receipt through a
  `persist: &mut dyn FnMut(&ReconcileReceipt) -> anyhow::Result<()>`
  argument. The command passes a closure over `receipt::write_atomic` and
  the validated path. Tests pass a recorder that keeps each persisted
  receipt and fails at a chosen write. No filesystem fault can target the
  write that follows the committed reset, so this argument is the test hook.
- **Failure.**
  - A Qdrant error stops the batch loop, leaves `complete: false`, prints
    the error with the receipt path, and exits nonzero.
  - A receipt rewrite failure after step 6 stops before the next mutation.
    It prints the confirmed `versions_reset` count and `deleted` IDs on
    stderr with the receipt path, and exits nonzero. The last persisted
    receipt stays `complete: false`. When the failing write is the final
    `complete: true` one, every delete has run and the exit is still
    nonzero.
  - A rerun starts over from the dry-run.
- **Output.** The JSON keys add `mode: "apply"`, `receipt`, `deleted_points`,
  `versions_reset` and `complete`. The `gcode-cli.md` version-13 note
  extends to the apply flags. `recovery.md` gives the apply command and
  states that it runs only through the Live Maintenance Procedure.

**Granularity:** nine production files, one outcome: guarded apply. The
receipt extraction exists only so apply and retire-files share one set of
location checks. Shipping it alone leaves no consumer, and shipping apply
without it duplicates security checks. Its twelve acceptance items are the
guards and failure boundaries of that one sequence.

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
- 2.2.7 - When the initial receipt cannot be written (a private receipt
  directory with mode 0500), apply exits nonzero, changes no flag and
  sends no Qdrant delete. test:
  `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs::apply_makes_no_mutation_when_initial_receipt_write_fails`.
- 2.2.8 - An existing receipt for a different digest is refused before any
  flag reset or Qdrant delete. test:
  `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs::apply_refuses_foreign_receipt_before_mutation`.
- 2.2.9 - A retained version with no file state that apply reset stays
  `vectors_synced = false` when this machine later adopts it, so the
  worker's pending query selects it. test:
  `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs::apply_reset_survives_adoption_of_retained_version`.
- 2.2.10 - When the receipt write right after the committed flag reset
  fails, apply exits nonzero and sends no Qdrant delete. The flags stay
  reset. Stderr gives the receipt path and the confirmed `versions_reset`
  count, and the last persisted receipt is the initial one. test:
  `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs::apply_stops_before_deletes_when_reset_receipt_write_fails`.
- 2.2.11 - With 257 orphan IDs (two batches), when the write after the first
  batch fails, apply exits nonzero and sends no second delete. Stderr lists
  the first batch's IDs. The last persisted receipt has `versions_reset`
  filled, `deleted: []` and `complete: false`. test:
  `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs::apply_stops_after_first_batch_when_receipt_write_fails`.
- 2.2.12 - When only the final `complete: true` write fails, apply exits
  nonzero after every delete. The last persisted receipt lists every batch
  with `complete: false`. test:
  `crates/gcode/src/commands/vector/reconcile/tests/serial_db.rs::apply_exits_nonzero_when_final_receipt_write_fails`.

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
  2. `git log --all --since-as-filter=<N>.days --format=%T` gives the
     unique root trees. `--since-as-filter` visits every reachable commit,
     so a recent ancestor of an older-dated tip is kept (S10). Plain
     `--since` stops traversal there. Installed Git is 2.54.0.
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
  3.2 narrows this to the root's eligible candidates.
  `recent_content_hashes_in_git_history` is deleted.

Planned verification:
`cargo nextest run -p gobby-code -E 'test(history)'`, then
`GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test cargo nextest run -p gobby-code -E 'test(content_gc)'`,
then `cargo clippy -p gobby-code` and `cargo fmt -p gobby-code -- --check`.

**Granularity:** two production files and seven acceptance items, one
outcome: history protects a version only at its own path. The items are the
walk's path, rename, encoding, filter, failure, object-format and traversal
cases.

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
  of that root. Its fixture paths are eligible under `test_context()`, so the
  test still passes after 3.2. test:
  `crates/gcode/src/commands/status/content_gc/tests.rs::history_failure_retains_root_candidates`.
- 3.1.6 - A sha256 repository is walked correctly. test:
  `crates/gcode/src/commands/status/content_gc/history/tests.rs::sha256_repository_tree_walk`.
- 3.1.7 - A commit dated inside the window, followed on the same branch by
  a tip dated before the cutoff, still protects its path and hash. test:
  `crates/gcode/src/commands/status/content_gc/history/tests.rs::recent_ancestor_behind_old_tip_stays_protected`.

### 3.2 Current-config eligibility for history protection [category: code] (depends: 3.1)
`kind: deliverable`

Targets:
- `crates/gcode/src/index/walker/classification.rs::passes_path_filters`
- `crates/gcode/src/index/walker/classification.rs::explicit_path_visible`
- `crates/gcode/src/index/walker/classification.rs::classify_explicit_file_with_options`
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
    `explicit_path_visible` holds with no size limit.

  A missing path is never gitignore-excluded. Content checks (generated
  bundle, size) are skipped.
- `explicit_path_visible` sets `max_filesize = Some(MAX_FILE_SIZE)`
  (`classification.rs:105-107`), so a large current file would fail the
  visibility walk. It gains a `max_filesize: Option<u64>` parameter. Its
  only existing caller, `classify_explicit_file_with_options` (`:57`),
  passes `Some(MAX_FILE_SIZE)`, which keeps indexing unchanged.
  `HistoryPathEligibility` passes `None`.
- `effective_excludes` becomes `pub(crate)`, re-exported from `indexer`.
- `discover_content_gc(ctx: &Context, retention_days)` takes the project from
  `ctx.project_id` and decides each candidate root in this order:
  1. A root that is not an existing directory retains all its candidates,
     eligible or not.
  2. Under an existing root, an ineligible candidate is collected without a
     history lookup.
  3. History (3.1) runs only for eligible candidates. A history error
     retains them, and the ineligible ones are still collected, because
     eligibility reads only config and the working tree.
- `prune.rs::discover_project_scoped_records` passes `&ctx`.
- Worktrees: each project root follows these rules with its own config. A
  root whose checkout is gone retains its candidates through rule 1.
  Stale-project prune owns removing that root.

Planned verification:
`cargo nextest run -p gobby-code -E 'test(classification)'`, then
`GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test cargo nextest run -p gobby-code -E 'test(content_gc) | test(prune)'`,
then `cargo clippy -p gobby-code` and `cargo fmt -p gobby-code -- --check`.

**Granularity:** six production files and seven acceptance items, one
outcome: history protects a path only while current config indexes it. The
items are that rule's eligibility cases and its order against root
availability and history failure. Shipping the rule without that order
would collect excluded paths under a gone root.

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
- 3.2.5 - An existing file that is not ignored and is larger than
  `MAX_FILE_SIZE` stays eligible with `respect_gitignore` set, so history
  at that path still protects an old candidate. Classification still
  excludes that file from indexing. test:
  `crates/gcode/src/index/walker/tests/classification.rs::history_eligibility_ignores_file_size`.
- 3.2.6 - A root that is not an existing directory retains an eligible
  candidate, a candidate under `extra_excludes`, and a hidden candidate
  outside the allowlist. test:
  `crates/gcode/src/commands/status/content_gc/tests.rs::gone_root_retains_every_candidate`.
- 3.2.7 - Under an existing root whose history cannot be read, a candidate
  under `extra_excludes` is collected and an eligible candidate is
  retained. test:
  `crates/gcode/src/commands/status/content_gc/tests.rs::history_failure_still_collects_ineligible_candidates`.

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
- The apply text says the flag reset makes versions repairable. A version
  drains through the worker of a machine whose file state references it,
  and a retained version stays pending until it is adopted.
- Remove the claim that `gcode prune` composes projection cleanup.
- The prune paragraph says history protects a version only at its own path,
  and only while the path passes the current excludes, hidden and gitignore
  rules. A root whose checkout is gone keeps all its versions, and a
  history error keeps the eligible ones.

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
   and handled by the next pass. The reset versions now report as pending:
   - `pending_current` falls as the workers of the referencing machines
     drain. Recheck it after this machine's worker drains, and report any
     remainder with its version IDs.
   - `pending_retained` stays until adoption and is not a failure.

   Send the receipt path and the verification output to the Orchestrator.

## Rollout
`kind: framing`

1. Leaves land through the lane review path. Each crate change becomes
   live only after a rebuild and promotion through `promote_workspace_binary_set`.
   The 1.2 Python change becomes live only after a daemon restart, which
   happens only with the Orchestrator's global announcement.
2. 1.1, 1.4 and 1.2 are safe in either activation order:
   - With the old gcode and the new worker, the old native commands still
     mark completion. A graph completion the old CLI loses leaves the row
     pending, because its compare-and-set dirties the current row.
   - With the new gcode and the old worker, the S2 and S6 races persist
     until the restart, and nothing breaks.
3. Run Live Maintenance Procedure step 0 before promoting the gcode that
   carries P3. P3 changes what the hourly prune collects.
4. Run the Live Maintenance Procedure after P2 is promoted. D1 waits for its
   receipt.

## V1 Plan Changelog
`kind: verification`

- 2026-10-05 14:52 CDT: First draft by the Lane 7 Plan Writer gobby#15429 on
  #22958. Design choices:
  - The missing-vector repair is hub-wide per version, following the #19743
    shared-hub topology.
  - The dry-run evidence is JSON on stdout.
  - The completion fix is split into the Rust compare-and-set (1.1) and the
    Python worker change (1.2).
- 2026-10-05 15:35 CDT: Enhancer pass applied. plan-enhancer-taskless-old
  ran once (run b54d93ac) on `05371c4296`. The Orchestrator gobby#14972
  accepted all four suggestions and folded the two graph routing items into
  this plan:
  - E1: 2.2 checks an existing receipt and writes the initial receipt
    before any mutation, and defines rewrite failure (2.2.7, 2.2.8).
  - E2: history eligibility drops the visibility walk's size limit (3.2.5).
  - E3: Decision Record 3, the P2 goal, the live verify step and 4.1 state
    repair liveness. Pending splits into `pending_current` and
    `pending_retained` (2.2.9).
  - E4: same-ID text-change tests (1.3.7).
  - Graph parity: S6 and S7 in As-Is Facts, Decision Records 12 and 13,
    new leaf 1.4, 1.2 covers both Python marks, and 1.3 resets both flags
    (1.3.8).
- 2026-10-05 15:39 CDT: The Orchestrator gobby#14972 folded in the
  worker's graph eligibility. S8 records the gap. Decision Record 13 now
  routes every graph-language file to gcode's `has_no_graph_facts`, and
  corrects its earlier claim: `sync_no_fact_file` only deletes. 1.2 drops
  the `symbol_count > 0` check and narrows the shortcut compare-and-set to
  `language` (1.2.5, 1.2.6).

## V2: Verification
`kind: verification`

Each leaf runs its planned verification after its final edit. After the last
leaf lands:

```bash
cargo nextest run -p gobby-code
GCODE_POSTGRES_TEST_DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_gcode_test cargo nextest run -p gobby-code -E 'test(serial_db) | test(mark_vectors_synced) | test(mark_vector_sync) | test(mark_graph_synced) | test(api_tests) | test(dirty_version_sync) | test(commands::graph) | test(history) | test(content_gc) | test(prune) | test(reconcile) | test(retire_files)'
cargo clippy -p gobby-code && cargo fmt -p gobby-code -- --check
DATABASE_URL=postgresql://gobby_test:gobby_test@127.0.0.1:60892/gobby_test GOBBY_TEST_PROTECT=1 uv run pytest tests/code_index/test_sync_worker.py tests/code_index/test_sync_worker_breaker.py tests/test_runner_code_index_shutdown.py tests/code_index/test_code_index_storage.py tests/skills/test_reference_library.py -q
uv run ruff format --check src/ && uv run ruff check src/ && uv run mypy src/
uv run gobby plans validate .gobby/plans/code-index-vector-reconciliation.md -p /Users/josh/Projects/gobby
```
