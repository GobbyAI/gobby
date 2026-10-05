# Code-index storage gaps (2026-09-26, Researcher #14550)

This investigation was read-only. VERIFIED means measured with a query or command. INFERRED means the claim is reasoned but not traced.

## Sizes (VERIFIED)

The hub DB `gobby` is 7.9 GB. The code index takes about 4.6 GB of it:

| Table | Size | Notes |
| --- | --- | --- |
| `code_calls` | 2.08 GB | The unique index `code_calls_unique_call_target` (9 columns) is 976 MB, larger than the 864 MB heap. |
| `code_content_chunks` | 1.64 GB | The BM25 index is 753 MB. |
| `code_symbols` | 0.84 GB | |

- Qdrant uses 1.5 GB allocated but reports 12 GB apparent size (the files are preallocated). The gobby collection is 1.1 GB.
- Overlay project IDs are worktree overlays. They match `code_indexed_projects` 1:1, and the hourly prune reaps them. They are not deleted-project leftovers; an earlier claim that they were is RETRACTED.

## Gap 1: Qdrant vectors are never deleted when their symbols disappear (VERIFIED counts; INFERRED cause)

Collection `code_symbols_d45545c5…`:
- 261,954 points against 183,097 PG symbols.
- 91,028 points have a `symbol_id` absent from PG, which is 35% of the collection:
  - 81,839 have provenance (`gcode`, `EXTRACTED`). 73,135 of those sit on paths that are still indexed, so they are superseded symbol IDs left behind after a file changed.
  - 9,189 have no provenance and no path: a legacy payload schema.
- 12,171 PG symbols have no vector.

Cause, SUPERSEDED: the first hypothesis was that PG symbol rows are removed through the FK cascade or a reindex path without a matching Qdrant delete. The trace below refutes it for the bulk of the orphans.

### Gap 1 trace (2026-09-26)

VERIFIED from code and git history:
- Commit `9d7ce3509f` ([gobby-#19743], 2026-08-06) made two changes together:
  - It added `file_content_hash` to `Symbol::make_id` (`crates/gcode/src/models.rs:159`). The key is `project:path:content_hash:name:kind:byte_start`, UUID5 in namespace `c0de1de0-0000-4000-8000-000000000000`.
  - It removed `delete_stale_vectors(file_path, keep_point_ids)` from `CodeSymbolVectorLifecycle::sync_file_symbols` and from `rebuild_symbols` (`crates/gcode/src/vector/code_symbols/lifecycle.rs:76`, `:130`).
- Since then, vector sync (`projection/sync.rs` `VectorProjectionState::sync_file`) only upserts. The only symbol-vector deletes left are content GC's `delete_candidate_projections`, project collection drop, and `gcode vector cleanup-orphans`. The last one removes only unindexed paths and has no scheduled caller in `src/gobby`.
- `delete_vectors_for_filter_excluding_ids` is now called only with an empty keep list, so its keep-list parameter is unused.
- The test `crates/gcode/src/vector/tests/projection.rs` asserts that sync issues no DELETE.
- Content GC deletes vectors before the PG row (`content_gc.rs:177` and `:288`). The current production code has no other PG version-row delete: `invalidate` deletes only per-machine selector and state rows, in both Rust `indexer::invalidate` and Python `delete_project_index`.

VERIFIED by read-only scripts (`orphan_versions.py`, `orphan_age.py`, `orphan_scheme.py`):
- All 81,839 orphans that carry a path have payload `project_id` equal to gobby. None come from worktree overlays.
- None of them matches a surviving PG `code_indexed_files` version by UUID5 recompute.
- None matches any git blob version of its path in the last 3, 14, 30 or 60 days on `--all`. The recompute was validated against live symbols, and `content_hash` equals the SHA-256 of the file bytes.
- 54,359 (66%) match the pre-#19743 scheme `project:path:name:kind:byte_start`, which has no content hash. They are vectors from before the ID-scheme switch that were never deleted after re-sync under the new IDs.
- The remaining 27,480 carry content-addressed IDs of unknown version. Their hashes match no committed blob within 60 days. INFERRED: they are uncommitted working-tree versions whose rows content GC removed, or they come from a GC/sync race. This is not proven.
- 70,571 orphans share path, name and kind with a live point, so they are duplicates that compete with live results in semantic search.

Fix direction, INFERRED:
1. Do a one-time cleanup of points whose `symbol_id` is absent from `code_symbols` of the same project, by id batch. This is safe because PG is authoritative.
2. Add a scheduled full reconcile of ids (Qdrant minus PG). Alternatively, extend `vector cleanup-orphans` to diff ids and not just paths, and have the hourly prune call it.
3. Do not restore the per-file keep-list delete. The content-versioned model deliberately keeps other versions' vectors alive.

## Gap 2: Content GC collects nothing (VERIFIED)

- Daemon retention is 1 day (`code_index.content_retention_days=1`). The hourly `gobby:code-index-prune` completes, but every run logs `Content GC: 0 version(s)`.
- The candidate SQL in `discover_content_gc` (`crates/gcode/src/commands/status/content_gc.rs`) returns 1,314 gobby versions. All 1,314 are then retained by `recent_content_hashes_in_git_history`. That function hashes every blob in the trees of every commit from the last day on `--all` refs; 100 or more commits a day covers about 13k blobs. It matches by hash only, ignoring path.
- The retained versions fall into three buckets (files / symbols / calls / chunks):

  | Bucket | Files | Symbols | Calls | Chunks |
  | --- | --- | --- | --- | --- |
  | Old versions of live paths | 563 | 22,088 | 67,454 | 3,419 |
  | `crates/gterminal/vendor/libghostty-vt` (now excluded, but still in HEAD, so protected forever) | 720 | 1,981 | 11,535 | 2,010 |
  | Paths no longer indexed | 168 | 1,310 | 1,712 | 1,273 |

- Across all projects, 1,867 unreferenced file versions hold 38k symbols, 198k calls and 11k chunks.

Fix direction, INFERRED:
- Protect only the (path, hash) pairs that the index would still ingest.
- Drop excluded paths regardless of git history.

## Smaller items (VERIFIED)

- `agent_runs` has dropped column 29, probably `summary_markdown` (unconfirmed). Its data persists in TOAST at about 65 MB until the rows are rewritten. `sessions` has about 20 dropped columns and 61 MB of TOAST.
- The event tables have no visible pruning:
  - `token_events` (294 MB) holds data since 2026-08-03 and has 65k dead rows.
  - `unmodeled_observation_events` is 414 MB.
- Off-DB disk: `cache/cargo-target-v2` 40 GB, `cache/cargo-target` 21 GB (likely stale), `worktrees` 22 GB, `backups/hub` 8.7 GB.

Scripts: `gc_filter_check.py` and `qdrant_orphans.py` in the session scratchpad.
