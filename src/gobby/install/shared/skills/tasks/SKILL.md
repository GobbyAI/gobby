---
name: tasks
description: Use when creating, claiming, implementing, reviewing, transitioning, or closing Gobby tasks.
version: "1.1.0"
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
| `validation_criteria` | Required for every `task_type` except `"epic"`, whatever the category; state observable, specific, complete behavior |

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
- A defect outside the claimed task's scope: `create_task` with `claim=true`
  and fix it in this session. Asking the user whether to fix is prohibited;
  deferral is only by explicit user order, when the surface belongs to an
  unlanded epic or another session (file the task naming that owner), or when
  the fix would touch another session's uncommitted files (message the owner
  instead — see the shared-worktree exclusion).
- Never run the full test suite unless the user explicitly requests it.
- Check current and projected line counts before touching applicable production
  source. Exactly 1,000 lines violates the ceiling. Load `decompose-monolith`
  for threshold-crossing work and finish the decomposition inside the current
  claimed task and session. Deferred refactor tasks are prohibited.

## Completion Gates

Closing a leaf task is an ordered checklist:

| # | Gate | Exception |
| --- | --- | --- |
| 1 | At least one linked commit when the task has attributed edits | No-edit close |
| 2 | No uncommitted task-attributed files | None |
| 3 | A clean category-appropriate validation command in a task session transcript | Docs, planning, research, manual, or no-edit close |
| 4 | One bounded criteria review | Organizational parent (no open children) |

An epic or other structural parent is closable when it has no open children.
Closing the last child auto-closes eligible ancestors in the same call — do not
walk the tree by hand.

Workspace rule: a task that owns an isolation worktree is not finished until
that worktree is landed and deleted (`merge_worktree` then `delete_worktree`;
after a manual merge, `mark_worktree_merged` then `delete_worktree`) or
explicitly handed off with `release_worktree`. Never leave a merged worktree
registered as active — see the `source-control` skill, Part 2: Landing a Task
Branch.

When a completion gate reports foreign-attributed dirt after its owning session
committed or abandoned the work, ask that owner to call
`release_task_paths(task_id="#N", paths=["path/to/file"])` on `gobby-tasks`.
Only the owning session can release attribution, and the tool refuses paths with
uncommitted content.

The close tool derives validation evidence from the claiming and closing session
transcripts. A later task-attributed file edit makes earlier validation stale;
commits preserve it. Shell validation must produce a definitive exit code, so
follow every yielded cell or PTY session until exit.

Use the verification commands from `.gobby/project.json`, scoped to touched
files and behavior. Code, refactor, and test tasks need a clean test-category
run. Config tasks need any clean validation command. When a CLI cannot expose a
definitive exit code, rerun the command through a supported shell tool.

## Exact Interactive Close Sequence

Follow this order exactly:

1. Finish all file edits.
2. Run focused validation after the final edit.
3. Fix every encountered error, warning, and failure; rerun validation to success.
4. Stage specific files and commit with
   `[<project_name>-#<task_number>] <type>: <description>`.
5. Review session memories; create, update, or delete valuable durable facts.
6. Call `close_task` once with `task_id`, `commit_sha`, `changes_summary`, and
   `preview=true`. A ready call links the commit and closes atomically.

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

Blocked calls remain read-only and return the first actionable checklist
failure. Repair that fact before retrying. Stale task state returns
`stale_task_state`; it never silently reruns the criteria review. Never call
`link_commit` merely to close.

## Review and Non-Work Paths

Interactive sessions use `close_task`; the present user is the reviewer.
Autonomous agents use the stage-specific tools on `gobby-tasks-ops`.

- Open `references/review-flows.md` before `submit_for_review`,
  `approve_review`, or `reject_review`.
- Open `references/no-work-closures.md` before closing as `duplicate`,
  `already_implemented`, `wont_fix`, `obsolete`, or `out_of_repo`.

## Memory Rule

Use `gobby-memory` for durable codebase facts, decisions, conventions, and stale
memory cleanup. A bug you find becomes a claimed task you fix now — a task, never
a memory. Memory maintenance is independent of task transitions.
