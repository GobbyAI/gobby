---
name: memory
description: Use Gobby's persistent memory for durable cross-session knowledge, recall, and stale-memory maintenance while keeping tasks, plans, code, and git authoritative for their own concerns.
version: "1.1.0"
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

## Recall

Gobby automatically recalls memories for each eligible parent-user turn. It makes
one attempt per turn, injects at most three results, and suppresses memory IDs already
injected during the current context epoch. Use recalled knowledge when it applies.

Automatic recall is a bounded first pass. Call `search_memories` when injected recall
is absent, too shallow, or insufficient for the work. Search again before creating a
memory so you can update, replace, or avoid duplicating an existing entry.

## Capture

Create a memory when either condition holds:

- The user explicitly asks Gobby to remember durable information.
- You learn a non-obvious fact, preference, convention, relationship, external
  reference, or finalized rationale whose rediscovery would require multi-step work.

Do not generalize a one-time instruction into a standing convention. Keep memories
specific, contextual, and time-resilient. A useful durability check is whether an
unrelated session would still benefit in three months.

`create_memory` requires a rationale of at most 500 characters. The rationale argues
why a future unrelated session should receive the memory; it is different from a
summary of the content. One-time outcomes, run IDs, and dated status rarely support a
durable rationale.

Prevent duplicates. Search first, review `similar_existing`, update the existing
memory when its identity remains valid, or supersede stale entries when a durable
decision replaces them.

## Maintenance

Recall is evidence, not authority. When a memory conflicts with current truth:

- Update it when the durable subject remains the same and its content changed.
- Delete it when it is obsolete, misleading, duplicated, or cheaply derivable.
- Create a replacement with `supersedes` when provenance of the durable change matters.

Use `review_task_memories(task_id, changes_summary)` after the post-close prompt for a
worked leaf. It searches memories related to the closed task without hiding memories
already recalled in this context. Update or delete stale candidates, and create a new
memory only when durable knowledge warrants one. Use the returned `source_task_id` for
any new memory. Zero candidates is a complete and valid review.

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
