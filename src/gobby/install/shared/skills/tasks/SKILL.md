---
name: tasks
description: Use when creating, claiming, implementing, reviewing, transitioning, or closing Gobby tasks.
version: "1.2.0"
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
- A defect outside the claimed task's scope follows the Found Work ladder
  below.
- Never run the full test suite unless the user explicitly requests it.
- Check current and projected line counts before touching applicable production
  source. Exactly 1,000 lines violates the ceiling. Load `decompose-monolith`
  for threshold-crossing work and finish the decomposition inside the current
  claimed task and session. Deferred refactor tasks are prohibited.

## Found Work

A defect you find during any task — broken behavior, a failing check, an
error in committed code — follows this ladder, in order:

1. Fix it now: `create_task` with `claim=true`, fix, close. Finding it is
   the authorization; an out-of-scope bug is never a scope change that
   needs user approval.
2. Surface owned by an active session — their uncommitted files, their
   in-flight work: hand it off. Send the failing command, diagnostics, and
   paths via `gobby-agents:send_message`; never touch their uncommitted
   files. Handoff is a fix path, and a passing scoped rerun against owned
   or clean paths clears your close gates.
3. File for the user — last resort, edge cases only: the fix needs a
   genuine architecture or product decision, or has a blast radius that
   needs a clean window. Label the task `needs-decision` or `clean-window`
   and state why in the description.

Operational friction — restarting a shared service, rebuilding a tool,
waiting for a quiet window — is coordination inside step 1, never a reason
to drop to step 3. An explicit user instruction to defer always stands.
Never end a turn asking "should I fix this?", and never go silent about a
finding; silence is worse than asking. Enhancement ideas with nothing
broken are not found work — note or file them normally.

## Completion Gates

Closing a leaf task is an ordered checklist:

| # | Gate | Exception |
| --- | --- | --- |
| 1 | At least one linked commit when the task has attributed edits | No-edit close |
| 2 | No uncommitted task-attributed files | None |
| 3 | A clean category-appropriate validation command in a task session transcript | Docs, planning, research, manual, or no-edit close |
| 4 | One bounded criteria review | Organizational parent (no open children) |

An epic, or a parent that owns no work of its own, is closable when it has no
open children. A claimed task or one with linked commits closes as a leaf even
after it gains found-work children — its own gates apply.
Closing the last child auto-closes eligible ancestors in the same call — do not
walk the tree by hand. The walk stops at an ancestor that is claimed, has an
open child, or still owes stage-manifest work; a claimed ancestor is in-flight
work its owner closes through its own gates.

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

The close tool derives validation evidence from the transcripts of the claiming
and closing sessions plus every earlier session that claimed or worked the task,
each within its own link window — an implementer's red/green run still counts
after it hands the task to a QA session. A later task-attributed file edit makes earlier validation stale;
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
5. Call `close_task` once with `task_id`, `commit_sha`, `changes_summary`, and
   `preview=true`. Include exact validation commands and results in `changes_summary`.
   A ready call links the commit and closes atomically.
6. Call `review_task_memories` after `closed=true`, passing the closed task and
   the same `changes_summary`; create, update, or delete only valuable durable facts.

Stage and commit only the files for this task:

```bash
git add <specific-files>
git commit --only -m "[<project_name>-#<task_number>] <type>: <description>" -- <task paths>
```

Always scope the commit as `git commit --only -- <task paths>` (with message options
before `--`). Only the named task paths enter the commit; foreign staged entries remain intact.

Preview after validation and commit:

```python
call_tool("gobby-tasks", "close_task", {
    "task_id": "#42",
    "commit_sha": "abc1234",
    "changes_summary": (
        "Protected explicit retrievals and consolidated task guidance. "
        "Validation: `uv run pytest tests/tasks/test_retrieval.py -q` -> 12 passed."
    ),
    "preview": True
}, session_id="#2333")
```

Blocked calls remain read-only and return the first actionable checklist
failure. Repair that fact before retrying. Stale task state returns
`stale_task_state`; it never silently reruns the criteria review. Never call
`link_commit` merely to close.

The criteria review runs once per evidence state, not once per attempt: its
verdict is memoized against the review and evidence fingerprints, so retrying
an unchanged close serves the stored verdict instead of paying another
provider round trip. Repair the blocker — a new commit, a fresh edit, corrected
criteria — and the changed fingerprint earns a fresh review on its own.

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
a memory. Task-specific review follows successful `close_task` because
`review_task_memories` requires a closed task.
