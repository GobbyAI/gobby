---
name: memory
description: Use Gobby's persistent memory for durable cross-session knowledge, search-first retrieval, and stale-memory maintenance while keeping tasks, plans, code, and git authoritative for their own concerns.
version: "1.2.1"
category: core
alwaysApply: false
triggers: remember, recall, forget, memory
metadata:
  gobby:
    audience: all
    format_overrides:
      autonomous: full
---

# Memory

Use only the `gobby-memory` MCP server for persistent memory. Gobby disables
provider-native memory files; never read or write them.

Most turns and most completed tasks need no memory write. Memory is for knowledge
that remains useful across unrelated future sessions and would take meaningful
exploration to rediscover.

## Search

Memories reach a session only through `search_memories`; nothing arrives on its own.
Search at these points:

- At task claim: search the task subject before editing (the claim nudge names the
  query).
- Before working in an unfamiliar subsystem.
- Before creating a memory, so you can update, replace, or avoid duplicating an
  existing entry.

Every hit carries `rationale`, `similarity`, and `memory_type`. Judge relevance from
the rationale and content; `similarity` ranks candidates within one search and is a
weak absolute signal because most raw scores fall in a narrow 0.62-0.75 band. Search
results are evidence, not authority.

## Capture

Incorrect runtime behavior is found work. Call `gobby-tasks.create_task`
with `claim=true` and fix it. Do not `create_memory`. That includes wrong
status, a broken invariant, a live process with an expired row, a probe
surprise, and "how the system currently misbehaves."

Create a memory when either remaining condition holds:

- The user explicitly asks Gobby to remember durable information.
- You learn a non-obvious fact, preference, convention, relationship, external
  reference, or finalized rationale whose rediscovery would require multi-step work.

Do not generalize a one-time instruction into a standing convention. Keep memories
specific, contextual, and time-resilient. A useful durability check is whether an
unrelated session would still benefit in three months.

`create_memory` requires a rationale of at most 500 characters. The rationale argues
why a future unrelated session should receive the memory; it is different from a
summary of the content. One-time outcomes, run IDs, and dated status rarely support a
durable rationale. The rationale is embedded with the content, so it also decides
which searches find the memory.

Prevent duplicates. Search first, review `similar_existing`, update the existing
memory when its identity remains valid, or supersede stale entries when a durable
decision replaces them.

## Maintenance

When a memory conflicts with current truth:

- Update it when the durable subject remains the same and its content changed
  (`update_memory` requires a fresh rationale with new content).
- Delete it when it is obsolete, misleading, duplicated, or cheaply derivable.
- Create a replacement with `supersedes` when provenance of the durable change matters.

Use `review_task_memories(task_id, changes_summary)` after the post-close prompt for a
worked leaf. It searches memories related to the closed task. Update or delete stale
candidates, and create a new memory only when durable knowledge warrants one. Use the
returned `source_task_id` for any new memory. Zero candidates is a complete and valid
review.

## Persistence Boundaries

Choose the authoritative surface:

| Knowledge | Surface |
| --- | --- |
| Durable preference, convention, decision rationale, or hard-to-rediscover relationship | Memory |
| Bug, issue, or actionable follow-up | Task |
| Draft direction, implementation approach, enhancement suggestion, or review finding | Plan or evidence |
| Current behavior or structure | Code and tests |
| Change history and completed implementation | Git |

Never store bugs as memories. Claim and fix them through the task workflow. Avoid
memorizing tool schemas, file locations, current task status, completed fixes, or facts
already obvious from code, tests, project instructions, or git history.

## Tags and Provenance

Use a few content-derived tags such as `testing`, `security`, `architecture`,
`convention`, or `reference`. `source_task_id` and `created_by_agent` derive from the
active session; pass them only for an intentional override. A post-close review returns
the canonical closed `source_task_id` because the open-claim derivation no longer
applies.
