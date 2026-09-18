# Recall and navigate memory

Load at task claim, before unfamiliar subsystem work, or before capture.

A pushed `<memory-index>` lists up to five ranked hits, one per line: short ID,
type, the searches that found it, last-updated date, and content lead. The
closing `when:` clause is the lead of the memory's rationale; it says when the
memory applies. The index carries no scores. Fetch a hit whose clause matches
with `get_memory(memory_id=...)` before acting on it. The index is bounded to
its trigger moments, so it never replaces your own search.

Discover with `gobby-memory:search_memories`; use a subject query and inspect
content and rationale. Refine with type or `tags_all`, `tags_any`, `tags_none`
filters. `list_memories` browses the scoped live set; `get_memory` retrieves a
known ID; `get_related_memories` follows cross-references; `memory_stats` checks
inventory. `search_knowledge_graph` searches extracted entities when available.

Treat hits as evidence, not authority. Compare candidates within one search;
scores are not universal truth thresholds. `similarity` includes temporal decay,
while a positive `min_score` filters `undecayed_similarity`. Inspect diagnostics,
ranking provenance, and collapsed duplicates. A missing score is not proof of
irrelevance. Do not assume list results are an exhaustive export: the MCP list
has a limit and no offset; use the operator export/backup surface for that need.

If recall misses an expected entry, check current project and global visibility,
remove restrictive tags or thresholds, and try concrete identifying terms.
Inspect the returned failure before repairing infrastructure. Keyword fallback
can work without vectors; graph search requires the graph service. Load
[maintenance](maintenance.md) before changing secondary stores.

Oversized responses are successful offloads: read/search the stored result and
consume its pages. Keep its `recall_request_id` for diagnostics rather than
inventing or reusing telemetry IDs. `judge_shadow_relevance` is a lifecycle
diagnostic, not a replacement for reading and judging search hits yourself.

Guide: [Search](../../../../../../../../docs/guides/memory.md#search).

_Last verified: 2026-09-18_
