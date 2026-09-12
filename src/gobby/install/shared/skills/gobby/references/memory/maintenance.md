# Maintain and recover memory

Load for stale knowledge, missing secondary data, backup, or recovery. Start with
`gobby-memory:memory_stats`, scoped search, and the actual error. PostgreSQL owns
memory truth; Qdrant vectors and FalkorDB entities are secondary projections.
Distinguish a missing row from a missing projection before rebuilding anything.

For individual entries use update, supersession, or deletion under
[capture](capture.md). `restore_memory` restores a known owned soft-hidden entry
and its indices; it cannot undo hard deletion.

For infrastructure maintenance, inspect each schema and requested scope:

| Tool | Operation and boundary |
| --- | --- |
| `backup_memories` | Export current live project rows to the machine-local backup |
| `restore_memories` | Validate and upsert a project backup, preserving absent/newer database rows |
| `rebuild_crossrefs` | Recompute similarity edges for the selected bounded set |
| `rebuild_knowledge_graph` | Extract entities/relationships for the selected bounded set |
| `reindex_embeddings` | Regenerate stored memory vectors; no MCP project selector |
| `recluster_knowledge_graph_entities` | Offline entity clustering; requires graph service and clustering dependencies |
| `densify_knowledge_graph_cooccurrence` | Materialize derived co-occurrence support edges |
| `judge_shadow_relevance` | Lifecycle judging of pending recall candidates, not user knowledge capture |

Cross-reference and graph rebuilds accept an explicit project and default limit
500; omitted project is not guaranteed to mean the caller project. Clustering and
densification default to current project. Do not describe a bounded rebuild count
as proof that every memory was rebuilt, or a success envelope as proof that every
per-memory extraction succeeded. Inspect summaries and errors.

Operator-only `gobby memory` commands include Markdown `export`, JSONL
`backup`/`restore`, exact-content `dedupe`, `backfill-unscoped-lessons`, graph
`graph-counts`/`clear-graph`/`rebuild-graph`, `reconcile`, `reindex-embeddings`,
`rebuild-crossrefs`, and `invalidate`. Read command help before execution.
`dedupe --dry-run` previews same-project duplicates and keeps the earliest row;
apply hard-deletes duplicates. `invalidate` clears and rebuilds secondary indices;
it is broader than correcting one memory. Use isolated fixtures for verification.

Recall diagnostics are operator commands under `gobby memory recall-signals`:
`backfill-events`, `backfill-labels`, `gate`, `audit-labels`,
`supersede-legacy-cohort`, `drift`, and `replay-candidate-filter`. Inspect cohort,
caller, label provenance, and dates; historic automatic-recall data is not current
agent search traffic. Backfills and supersession mutate telemetry. They do not
repair memory content or justify changing ranking from one weak example.

If a backup is missing, create an explicit backup of the intended scope. If a
service is unavailable, recover its configuration/connectivity before retrying.
Coordinate daemon restarts with active sessions and honor protected dream runs.

Guide: [Maintenance](../../../../../../../../docs/guides/memory.md#maintenance),
[Backup format](../../../../../../../../docs/guides/memory.md#backup-format), and
[Troubleshooting](../../../../../../../../docs/guides/memory.md#troubleshooting).

_Last verified: 2026-09-12_
