---
name: tasks
description: Use when creating, claiming, implementing, reviewing, transitioning, or closing Gobby tasks.
version: "1.0.0"
category: core
triggers: create task, claim task, close task, submit for review, task transition, validation evidence
metadata:
  gobby:
    audience: all
    format_overrides:
      autonomous: full
---

# Tasks

Use `call_tool("gobby-tasks", ...)` for task lifecycle operations. Fetch a known
unleased tool schema with `get_tool_schema("gobby-tasks", "<tool>")` before its
first call. Use `gobby-tasks-ops` for autonomous review transitions, fetching a
known unleased schema with `get_tool_schema("gobby-tasks-ops", "<tool>")`
before the first call.

`session_id` belongs to the outer `call_tool`, alongside `server_name`,
`tool_name`, and `arguments`. Pass the current Gobby session ref explicitly.

## Create or Claim Before Editing

Create or claim the deliverable task before editing files. Use lifecycle MCP
tools; the `gobby tasks` CLI is an operator interface and does not update agent
workflow state.

Required creation fields:

| Field | Requirement |
| --- | --- |
| `title` | Always; use an imperative summary |
| `category` | Always |
| `implementation_domain` | Required for `category="code"` |
| `validation_criteria` | Required for `category="code"`; state observable, specific, complete behavior |

Create and claim in one call when starting new work:

```python
call_tool("gobby-tasks", "create_task", {
    "title": "Fix session cleanup on missing transcripts",
    "category": "code",
    "implementation_domain": "backend",
    "task_type": "bug",
    "priority": 1,
    "claim": True,
    "validation_criteria": (
        "Focused session cleanup tests cover missing transcripts and pass."
    )
}, session_id="#2333")
```

Claim existing work with:

```python
call_tool(
    "gobby-tasks",
    "claim_task",
    {"task_id": "#42"},
    session_id="#2333",
)
```

Open `references/creation.md` when selecting task types, categories, priorities,
labels, or writing expanded validation criteria.

## Implementation

- Preserve unrelated worktree changes.
- Run focused tests for behavior changes.
- Fix every error, warning, test failure, lint failure, and type error encountered.
- Never run the full test suite unless the user explicitly requests it.
- Every non-test `.py`, `.ts`, `.tsx`, or `.css` source file over 1,000 lines
  needs an existing refactor task or a newly created one left unclaimed.

## Completion Gates

All gates apply to interactive close and autonomous review:

| # | Gate | Exception |
| --- | --- | --- |
| 1 | Fresh verification after the final edit | None |
| 2 | Relevant changes committed | Approved non-work close |
| 3 | Commit linked by the lifecycle transition | Approved non-work close |
| 4 | Memory review completed | None |

Later edits invalidate earlier verification readiness. Git commits preserve
captured evidence. Shell validation must produce a complete terminal result;
follow every yielded cell or PTY session until exit.

Use the verification commands from `.gobby/project.json`, scoped to touched
files and behavior. Every command run while a task is claimed is captured as a
durable receipt. Manual evidence is limited to `manual_diff_review` and never
replaces a shell command outcome.

Open `references/evidence-provider-recovery.md` only when evidence capture is
missing, provider-specific command recovery is needed, or manual review evidence
must be recorded.

## Exact Interactive Close Sequence

Follow this order exactly:

1. Finish all file edits.
2. Run focused validation after the final edit.
3. Fix every encountered error, warning, and failure; rerun validation to success.
4. Stage specific files and commit with
   `[<project_name>-#<task_number>] <type>: <description>`.
5. Review session memories; create, update, or delete valuable durable facts, or
   explicitly clear the memory gate.
6. Set `memory_review_completed=true`.
7. Call `close_task` with `task_id`, `commit_sha`, `changes_summary`, and
   `preview=true`.
8. Repair every blocker and repeat the conditional close until `closed=true`.
   A ready call reevaluates current state, links the commit, and closes the task.

Stage and commit only the files for this task:

```bash
git add <specific-files>
git commit -m "[<project_name>-#<task_number>] <type>: <description>"
```

Preview after validation and commit:

```python
call_tool("gobby-tasks", "close_task", {
    "task_id": "#42",
    "commit_sha": "abc1234",
    "changes_summary": "Protected explicit retrievals and consolidated task guidance.",
    "preview": True
}, session_id="#2333")
```

Blocked calls remain read-only and return repair actions. Pass
`evidence_receipt_ids` when the response requests specific assigned receipts.
Never call `link_commit` merely to close; the successful conditional
`close_task(..., commit_sha=..., preview=true)` call links and closes atomically.

## Review and Non-Work Paths

Interactive sessions use `close_task`; the present user is the reviewer.
Autonomous agents use the stage-specific tools on `gobby-tasks-ops`.

- Open `references/review-flows.md` before `submit_for_review`,
  `approve_review`, or `reject_review`.
- Open `references/no-work-closures.md` before closing as `duplicate`,
  `already_implemented`, `wont_fix`, `obsolete`, or `out_of_repo`.

## Memory Rule

Use `gobby-memory` for durable codebase facts, decisions, conventions, and stale
memory cleanup. Bugs and errors belong in tasks. When nothing is worth changing:

```python
set_variable(name="memory_review_completed", value=true, session_id="#2333")
```
