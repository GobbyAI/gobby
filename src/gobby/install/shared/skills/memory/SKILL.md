---
name: memory
description: When and how to use Gobby's persistent memory system effectively. Covers decision frameworks for what to remember, how to write durable memories, and maintenance patterns.
version: "1.0.0"
category: core
alwaysApply: false
triggers: remember, recall, forget, memory
metadata:
  gobby:
    audience: all
    format_overrides:
      autonomous: full
---

# Memory — When, How, and Why

Gobby's memory system (gobby-memory MCP) stores persistent facts across sessions. Use progressive discovery for tool schemas — this skill teaches judgment, not API reference.

> **Note:** Claude Code's native memory system (~/.claude/projects/.../memory/) is disabled by Gobby rules.
> All memory operations go through **gobby-memory** MCP. Do not read or write to the native memory filesystem — those operations will be blocked.

---

## When to Use Memory

Memory is one of several persistence mechanisms. Pick the right one:

| What you learned | Store it as | Why |
|-----------------|-------------|-----|
| User preference or convention | **Memory** | Durable, cross-session, hard to rediscover |
| A bug or issue to fix | **Task** | Actionable, trackable, closeable |
| Implementation approach for current work | **Plan** | Scoped to conversation, structured |
| Draft direction, enhancement suggestion, or review finding | **Plan** | Belongs in the plan artifact or evidence |
| Something the code already shows | **Nothing** | Code is the source of truth |
| Something git log already shows | **Nothing** | Git is the source of truth |

**Rule of thumb**: Would rediscovering this require multi-step exploration in a future session? If yes, memorize it. If you can find it by reading code or running `git log`, don't.

## How to Write Durable Memories

Memories that survive are **specific, contextual, and time-resilient**.

**Good memories:**
- "Josh prefers squash merges from worktrees — an earlier session once created hundreds of micro-commits"
- "The PostgreSQL hub migration baseline is 260; versions below `_MIN_MIGRATION_VERSION` are intentionally unsupported."
- "Pipeline bugs tracked in Linear project INGEST"

**Bad memories:**
- "The auth module is in src/gobby/auth/" — code structure changes; `find` is faster
- "Fixed bug in task validation" — the fix is in git; the commit message has context
- "Currently working on skill audit" — ephemeral, only relevant this session

**Durability test**: Will this be useful in 3 months? If the answer depends on code not changing, it's not durable.

## Every Memory Needs a Rationale

`create_memory` requires a `rationale`: one or two sentences (max 500 characters) on
why a **future, unrelated session** should be re-served this memory. Missing, empty,
whitespace-only, and over-length rationales are rejected with `rationale_required`.
The MCP tool, the HTTP route, and `gobby memory create --rationale` all enforce it.

A rationale is not a summary of the content — it is the argument for the memory's
continued existence. Write the sentence a future session would need to hear to agree
the memory is still worth reading.

**Good:**

> **content:** "Gobby commits use `[gobby-#NNNNN] <type>: <summary>`; the types are fix, feat, refactor, chore, docs."
> **rationale:** "Every session that commits here needs this format, and it is enforced by hooks rather than stated anywhere obvious."

The claim is durable — it names a standing convention and says why rediscovery is
expensive. Nothing in it expires.

**Bad:**

> **content:** "Review run 57c41dbb found 3 blocking findings in plan 380b1294; resolved in round 2."
> **rationale:** "Records the outcome of the round-1 plan review."

Rejected, and the rationale is what exposes it: the only honest claim available for
this memory is that it describes one event that already finished. Hex IDs, run
numbers, and dated status are the tell. When the best rationale you can write
describes a one-time event, the finding belongs in the plan evidence or a task.

**Rationale test**: if it needs a past-tense verb and a specific identifier, you are
logging, not memorizing.

Rationales are visible at recall time — injected `<project-memory>` blocks render
`why: <rationale>` next to `memory_id`, so a weak claim is read by every session the
memory reaches.

### Provenance is derived, not passed

`source_task_id` and `created_by_agent` are filled in automatically:

- `source_task_id` — the open task your session currently has claimed (the most
  recently updated one, if you hold several).
- `created_by_agent` — your agent run's agent name, falling back to the session's CLI
  source for interactive sessions.

Pass either argument only to override the derived value. Derivation failures degrade
to `None` instead of failing the create.

### Rationale controls how long a memory lives

Dream's audit judges every candidate against its rationale, and a `delete` or
`refresh` verdict must quote or paraphrase that rationale and say why the claim no
longer holds. Two consequences:

- A rationale describing one-time state hands dream a citable reason to reap the
  memory as soon as that state passes.
- A legacy row with a `NULL` rationale is not deletable on that basis alone — a
  missing rationale is treated as absent evidence, never as a verdict.

Rationale quality is the main lever you have over a memory's lifetime.

## What to Remember

- **User preferences** — coding style, communication preferences, workflow choices
- **Conventions** — naming patterns, architectural decisions that aren't in docs
- **Non-obvious relationships** — "X depends on Y because of Z" where Z isn't documented
- **External references** — where to find things outside the repo (Linear projects, Slack channels, dashboards)
- **Design rationale** — why something was built a certain way, especially if counterintuitive

## What NOT to Remember

- **Code paths and file locations** — they change; use search tools
- **Recent git activity** — `git log` is authoritative
- **Bug fixes or solutions** — the fix is in the code, context in the commit
- **Anything in CLAUDE.md** — already loaded every session
- **Ephemeral state** — current task, temporary config, in-progress work
- **Plan content** — draft direction, enhancement suggestions, and review findings

## Tags

Tags enable precise recall. Extract them from content:

| Content signal | Tag |
|---------------|-----|
| Testing, fixtures, pytest | `testing` |
| Security, auth, permissions | `security` |
| Architecture, design decisions | `architecture` |
| User preferences, conventions | `convention` |
| External systems, integrations | `reference` |

Use `tags_all` for AND queries, `tags_any` for OR, `tags_none` to exclude.

## Anti-Patterns

- **Memorizing tool schemas** — progressive discovery handles this; schemas go stale in memory
- **Memorizing code structure** — files move; grep/glob is faster and always current
- **Creating duplicate memories** — always search before creating; `create_memory` returns similar existing memories for exactly this reason
- **Storing one-time instructions as conventions** — only save if the user explicitly asked to remember, or if rediscovery would be expensive

## Maintenance

Memories decay. Periodically:

- **Audit** — find stale, duplicate, or code-derivable memories that should be cleaned up
- **Cleanup** — remove memories that are no longer accurate or useful
- **Rebuild cross-references** — keeps the relationship graph between memories fresh
- **Reindex embeddings** — improves semantic search quality after bulk changes

Use progressive discovery to find the maintenance tools on gobby-memory when needed.

## Knowledge Graph

The knowledge graph extracts entities and relationships from memories into a searchable graph. Use it when you need to understand connections between concepts rather than searching for specific content.
