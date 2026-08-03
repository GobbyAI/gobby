# BM25 Index Verification (#19421, plan 5.4)

Date: 2026-08-03. Live hub `:60891`, PostgreSQL + ParadeDB pg_search.
Method: `EXPLAIN (ANALYZE, COSTS OFF)` on each index's live query shape,
executed read-only against the live hub, followed by an immediate re-read of
`pg_stat_user_indexes.idx_scan` for every BM25 index.

## Hypothesis under test

~750 MB of pg_search BM25 indexes report `idx_scan = 0` while gcode
search/search-text, gwiki search, and the daemon keyword-search paths exercise
BM25 daily. Either the indexes are dead weight, or ParadeDB's custom scan
bypasses the standard PostgreSQL scan counter.

## Query shapes

Each shape is the production SQL arm that owns the index:

| Index | Consumer | Shape source |
| --- | --- | --- |
| `code_symbols_search_bm25` | gcode `search-text` symbols arm | `crates/gcode/src/search/fts/symbols.rs` (`name/qualified_name/signature/docstring/summary @@@ $q` + project filter) |
| `code_content_search_bm25` | gcode `search-content` chunks arm | `crates/gcode/src/search/fts/content.rs` (`content @@@ $q` + project filter) |
| `tool_result_chunks_search_bm25` | daemon keyword search | `src/gobby/search/keyword.py` (`content @@@ $q`) |
| `gwiki_documents_search_bm25` | gwiki search documents arm | `crates/gwiki/src/search/bm25.rs` (`path/title/body @@@ $1`) |
| `gwiki_chunks_search_bm25` | gwiki search chunks arm | `crates/gwiki/src/search/bm25.rs` (`path/content @@@ $1`) |
| `tasks_search_bm25` | task search | `src/gobby/storage/tasks/_search.py` (`title/description @@@ $q`) |
| `memories_search_bm25` | daemon keyword search | `src/gobby/search/keyword.py` (`content @@@ $q`) |
| `skills_search_bm25` | daemon keyword search | `src/gobby/search/keyword.py` (`name/description/content @@@ $q`) |

## Per-index verdicts

Every plan contains `Custom Scan (ParadeDB Base Scan)` with an explicit
`Index: <name>` line naming the index under test:

| Index | Size | Plan node | Index named in plan | Verdict |
| --- | --- | --- | --- | --- |
| `code_content_search_bm25` | 413 MB | ParadeDB Base Scan, 39.6 ms | yes | **stay** |
| `code_symbols_search_bm25` | 121 MB | ParadeDB Base Scan, 7.8 ms | yes | **stay** |
| `tool_result_chunks_search_bm25` | 89 MB | ParadeDB Base Scan, 16.7 ms | yes | **stay** |
| `gwiki_documents_search_bm25` | 55 MB | ParadeDB Base Scan, 134.9 ms | yes | **stay** |
| `gwiki_chunks_search_bm25` | 48 MB | ParadeDB Base Scan, 23.0 ms | yes | **stay** |
| `tasks_search_bm25` | 14 MB | ParadeDB Base Scan, 32.2 ms | yes | **stay** |
| `memories_search_bm25` | 9.1 MB | ParadeDB Base Scan, 13.2 ms | yes | **stay** |
| `skills_search_bm25` | 3.4 MB | ParadeDB Base Scan, 10.9 ms | yes | **stay** |

## Counter-bypass proof

Immediately after the eight `EXPLAIN (ANALYZE)` executions above — each of
which physically scanned its index — every `pg_stat_user_indexes.idx_scan`
value for the BM25 indexes still read **0**. ParadeDB's custom scan node does
not increment the standard scan counter. `idx_scan = 0` on a BM25 index
carries no evidence of disuse; plan-shape inspection is the only valid
liveness probe. This is the ParadeDB counterpart of plan 2.3's btree evidence
rule.

## Disposition

All eight indexes stay. Migration `368_bm25_disposition.sql` ships as the
no-op verdict record reserved by the allocation manifest (slot re-serialized
2026-08-02 under #19378), keeping the migration chain contiguous with the
M0 range (369+) unchanged.
