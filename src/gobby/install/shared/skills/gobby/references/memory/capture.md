# Capture durable knowledge

Load before creating or changing memory. Search first; inspect existing entries
and their rationale. Capture only an explicit durable remember request or
non-obvious knowledge useful across unrelated future sessions. Do not turn a
one-time instruction into a permanent preference. Avoid secrets and facts already
obvious from instructions, code, tests, or Git.

Use `gobby-memory:create_memory` with content and a rationale explaining why a
future unrelated session should receive it. Content is capped at 3,000 characters;
rationale at 500. Excess is rejected, never truncated. Condense the subject or
separate genuinely distinct knowledge; do not split a single oversized memory
merely to bypass the cap. Use a few content-derived tags.

Supported durable types are `fact`, `preference`, `pattern`, and `context`.
The special `implementation_note` ingestion path can skip ephemeral content;
successful `skipped` is not a stored memory. Do not use it for change history.
Task and agent provenance derive from the active session unless intentionally
overridden. After closure use the canonical `source_task_id` returned by review.

Inspect `similar_existing` and `auto_superseded` in the create result: the current
MCP create path probes five neighbors and can automatically supersede raw cosine
matches at or above 0.9. Similarity is a duplicate signal, not permission to ignore
different durable subjects. Explicit `supersedes` accepts at most 20 IDs.

Use `update_memory` when the same durable subject changes; a content change
requires a fresh rationale. Use `create_memory` with `supersedes` when replacement
provenance matters. Delete obsolete or misleading knowledge with `delete_memory`
only after checking the ID and owner: this is a hard delete. `restore_memory`
recovers soft-hidden rows, not hard deletion; backup recovery is separate.
On a cap, scope, or type error, correct the input rather than retrying unchanged.

Guide: [What belongs in memory](../../../../../../../../docs/guides/memory.md#what-belongs-in-memory)
and [MCP tools](../../../../../../../../docs/guides/memory.md#mcp-tools).

_Last verified: 2026-09-12_
