# Datastore growth findings — 2026-08-26

Task #20986. Measured on the hub host after the 03:02 CDT hub backup, daemon
PID 58239, all stores healthy. Sizes from `docker system df -v`, `du` inside the
containers and a throwaway `alpine` mount of the volume, `pg_total_relation_size`,
`pgstattuple`/`pgstatindex` (extension created and dropped for the measurement),
the Qdrant HTTP API, and `GRAPH.MEMORY USAGE`.

| Volume | Reported | Real bytes | Dominant content | Verdict |
| --- | --- | --- | --- | --- |
| `gobby_qdrant_data` | 18.48 GB | 3.9 GB | `code_symbols_<gobby>` 2.5 GB (84% stale versions) | reporting artifact + retention window |
| `gobby_pgaudit_log` | 9.04 GB | 8.5 GiB | 187 daily files, July 2026 = 6.94 GB | missing retention |
| `gobby_postgres_data` | 8.7 GB | 8.5 GiB | code-index tables 5.6 GB (80–85% stale versions) | retention window + churn bloat |
| `gobby_falkordb_data` | 435 MB | 416 MB | `dump.rdb`; graph `gobby_code` 1,502 MB in memory | as designed |

## Qdrant

- The 18.48 GB figure is apparent size: `docker system df` sums `stat` sizes and
  Qdrant preallocates sparse 32 MiB files (`payload_storage/page_0.dat`,
  `wal/open-N`). 385 such files carry 12.9 GB apparent for well under 1 GB real.
  A 2 MB collection shows ~590 MB apparent. `du` on the volume: 3.9 GB.
- Real bytes by collection: `code_symbols_d45545c5…` (gobby) 2.5 GB,
  `code_symbols_11f91c6e…` 448 MB, `code_symbols_f1fed93e…` (gobby-web) 295 MB,
  `gwiki_project_d45545c5…` 189 MB, `code_symbols_b571320a…` 167 MB,
  `code_symbols_8afc2302…` 93 MB, `memories` 64 MB — 3.76 GB of 3.9 GB (96%).
- `code_symbols_<gobby>` holds 709,839 points against 112,177 current symbols:
  84% are vectors for stale content-hash versions inside the 30-day
  `content_retention_days` window (#21029).
- Orphaned collections for 18 path-derived projects (deleted worktrees, an
  indexed wiki vault) ≈ 0.87 GB real (#21025).
- Separate from the volume: 29 `hub_backup_verify_*` snapshot files, 4.13 GB, in
  the container's overlay layer, left behind by the backup verify step. Deleted by
  hand today; fix in #21026.
- Test residue: `gwiki_topic_refresh-test`, `gwiki_topic_track-b-bakeoff`,
  `gwiki_topic_vverify`, `gwiki_topic_vision-smoke` (≈3 MB total). Left for the
  wiki re-implementation.

## PostgreSQL

`gobby` database 8,109 MB (`base/16384` 8.3 GiB; `pg_wal` 81 MB). Top relations
(total / heap / indexes / toast):

| Relation | Total | Heap | Indexes | Toast |
| --- | --- | --- | --- | --- |
| `code_calls` | 2,974 MB | 892 MB | 2,081 MB | — |
| `code_content_chunks` | 1,573 MB | 333 MB | 738 MB | 502 MB |
| `code_symbols` | 849 MB | 509 MB | 340 MB | — |
| `unmodeled_observation_events` | 263 MB | 62 MB | 201 MB | — |
| `loop_progress` | 254 MB | 187 MB | 67 MB | — |
| `code_imports` | 174 MB | 62 MB | 111 MB | — |
| `tool_result_chunks` | 159 MB | 51 MB | 101 MB | 7 MB |
| `memory_dream_snapshots` | 150 MB | 79 MB | 2 MB | 69 MB |
| `gwiki_chunks` | 146 MB | 54 MB | 69 MB | 22 MB |
| `spans` | 122 MB | 0 | 121 MB | 2 MB |

The top 25 relations sum to ≈7.5 GB (92%).

- Code index (`code_calls`, `code_content_chunks`, `code_symbols`, `code_imports`,
  `code_indexed_files`, `code_indexed_file_states`) = 5.65 GB. For the gobby
  project: 30,314 file-version rows for 14,479 distinct paths; stale versions are
  622,290 of 734,467 symbols (85%), 2,001,476 of 2,458,536 calls (81%),
  193,199 of 231,411 chunks (83%). The content GC (30 days on
  `last_referenced_at`) has had no eligible row yet — `min(last_referenced_at)` is
  2026-08-07 (#21029).
- `code_calls_unique_call_target` (9 columns, `NULLS NOT DISTINCT`) is 1,719 MB at
  46% leaf density / 49% fragmentation; `code_calls` heap 39% free,
  `code_content_chunks` 29%, `code_symbols` 21%. Churn bloat from version
  delete/reinsert; a one-off `REINDEX INDEX CONCURRENTLY code_calls_unique_call_target`
  would reclaim ≈0.8 GB and re-bloat over time. No task filed.
- Chunk content by top-level directory (gobby): `wiki/` 79,579 chunks / 743 MB,
  `.gobby/` 27,042 / 340 MB, `tests/` 59,975 / 199 MB, `src/` 28,929 / 97 MB,
  `crates/` 23,095 / 72 MB. `wiki/` is gitignored and rescued on purpose by the
  walker allowlist (`crates/gcode/src/index/walker/hidden.rs:85`); the codewiki
  redesign owns that decision.
- Orphaned path-derived projects: 20 of 24 `code_indexed_projects` rows have no
  `code_indexed_project_states` selector on any machine, so no sweep can see them
  (nightly "Stale project reconciliation" logs `scanned=0`). ≈0.6 GB of hub rows
  (#21025).
- `spans`: 0 rows, 1.5M inserts / 3.1M deletes, 121 MB of dead btree. Rebuilt today
  with `REINDEX TABLE CONCURRENTLY spans` → 24 KB.
- `loop_progress`: 721,534 rows, 711,421 for `expired` sessions, 15k–52k rows/day,
  `clear_session` has no caller (#21028).
- `token_events`: 177k rows / 81 MB since 08-03, ledger by design, ≈3.5 MB/day.
- Retention loops verified working: `unmodeled_observation_events` (30 d by
  `last_seen_at`, 0 rows older), `memory_dream_snapshots` (cascade from
  `prune_runs`, 28-day span), `metrics_events`, `workflow_audit_log`,
  `tool_results` (7 d).
- Residue in the production cluster: databases `gobby_test` and
  `gobby_gcode_test` (34 MB each, 0 backends, no code references).
- `gobby_agent_auth.principal_bindings` 5.3 MB / `principal_audit_events` 4.2 MB;
  live managed roles: 1 agent, 1 interactive. The 7,614 maintenance bindings are
  a count, not a size problem.

## pgaudit

187 daily files, 8.5 GiB: May 0.59 GB, June 1.18 GB, July 6.94 GB, August 0.33 GB.
The largest day (2026-07-12, 571 MB, 5.03M lines) is 99.9% `AUDIT: SESSION … DDL`
from per-test `CREATE SCHEMA gobby_test_*` / `DROP SCHEMA … CASCADE` when pytest
ran against the production cluster. August files contain zero `gobby_test_`
lines; current volume is migration DDL plus checkpoint lines, under 1 MB/day.
`log_rotation_age=1d` creates a file per day and nothing deletes one. Fix and the
one-time cleanup are in #21027 (needs Josh's retention choice and the
`block-docker-policy-edits` toggle).

## FalkorDB

Single `dump.rdb`, 435 MB. Graphs: `gobby_code` 1,502 MB in memory, `gobby_kg`
10 MB, `gobby_wiki` 1 MB, `gwiki` 0 MB. Rebuildable projection; nothing to fix.

## Applied today

- Deleted 4.13 GB of `hub_backup_verify_*` snapshots from `services-qdrant-1`.
- `REINDEX TABLE CONCURRENTLY spans` (121 MB → 24 KB).

## Follow-up tasks

- #21025 Purge orphaned path-derived code-index projects (Josh: sweep on missing worktree/branch).
- #21026 hub-backup: delete scratch Qdrant snapshots before dropping the verify collection.
- #21027 pgaudit retention (`needs-decision`).
- #21028 loop_progress 7-day prune.
- #21029 code-index content-version retention decision (`needs-decision`).
