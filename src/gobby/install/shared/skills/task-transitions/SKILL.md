---
name: task-transitions
description: "Pre-transition checklist for closing or reviewing tasks. Covers commit, lint/test, error triage, and memory gates."
category: core
metadata:
  gobby:
    audience: all
    format_overrides:
      autonomous: full
---

# Task Transition Checklist

Before closing or changing task status, complete ALL gates below. They fire in order — handle them all at once to avoid bouncing.

---

## Gate Summary

| # | Gate | Variable | Skipped by |
|---|------|----------|------------|
| 1 | Clean working tree | — | Non-work closes |
| 2 | Commit linked to task | — | Non-work closes |
| 3 | Lint + tests pass, all issues fixed | `errors_resolved` | — |
| 4 | Memory review completed | `memory_review_completed` | — |

**Non-work close reasons** (skip gates 1-2): `duplicate`, `already_implemented`, `wont_fix`, `obsolete`, `out_of_repo`

Gates 3-4 always apply, even for non-work closes.

---

## Gate 1: Clean Working Tree

All changes must be committed before status transitions.

```bash
git add <specific-files>
git commit -m "[project-#N] type: description"
```

Prefer staging specific files over `git add -A`. Include the task reference in the commit message.

## Gate 2: Commit Linked to Task

Pass `commit_sha` to `close_task` to link and close in one call:

```python
call_tool("gobby-tasks", "close_task", {
    "task_id": "#N",
    "commit_sha": "abc1234",
    "changes_summary": "What changed and why"
})
```

## Gate 3: Errors, Warnings, and Failures Resolved

Run the verification commands defined in `.gobby/project.json` on the files you touched. The `verification` key contains named commands — run the ones relevant to your changes:

```bash
# Read the project's verification config
cat .gobby/project.json | jq '.verification'

# Run relevant checks (examples — use the actual commands from project.json)
# verification.lint → linting
# verification.format → format checking
# verification.type_check → type checking
# verification.unit_tests → tests (scope to relevant files, not the full suite)
# verification.custom.* → project-specific checks (frontend, etc.)
```

Scope test runs to relevant files — do NOT run the full test suite unless explicitly asked.

Fix ALL errors, warnings, and failures — including pre-existing ones.

**Do NOT be lazy and simply file a task without first thoroughly investigating the issue.** If you can complete the fix this session without compaction, you must do it. The only exception is something that genuinely requires multi-session architectural changes across many files.

Once resolved:

```python
set_variable(name="errors_resolved", value=true, session_id="#session")
```

## Gate 4: Memory Review

Review your session for memories worth preserving:
- New insights about the codebase, user preferences, or design decisions
- Stale memories that need updating or deleting

If nothing new was learned and no stale memories remain, clear the gate:

```python
set_variable(name="memory_review_completed", value=true, session_id="#session")
```

Do NOT create memories for bugs or errors — create tasks instead.

---

## Complete Close Sequence (interactive sessions)

```
1. git add + git commit (with [project-#N] in message)
2. Run verification commands from .gobby/project.json on touched files → fix everything
3. set_variable(errors_resolved=true)
4. Review memories → save/delete/clear gate
5. set_variable(memory_review_completed=true)
6. close_task(task_id, commit_sha, changes_summary)
```

## Review Flow (autonomous/pipeline agents)

Autonomous agents **must** use the review flow — they cannot close tasks directly. The same gates apply.

### mark_task_needs_review — submit work for review

```python
call_tool("gobby-tasks", "mark_task_needs_review", {
    "task_id": "#N",
    "review_notes": "What was done and what to verify"
})
```

Commits are auto-linked from your session. All four gates still apply before this call succeeds.

### mark_task_review_approved — approve after review

```python
call_tool("gobby-tasks", "mark_task_review_approved", {
    "task_id": "#N",
    "approval_notes": "Verified: tests pass, changes match spec"
})
```

Used by QA agents after reviewing work. Same gates apply — if the reviewer made fixes and committed, those commits are auto-linked.

### Interactive vs Autonomous

| Context | Use | Why |
|---------|-----|-----|
| Interactive (user present) | `close_task` | User is the reviewer — no separate review step needed |
| Autonomous (pipeline/agent) | `mark_task_needs_review` | **Required** — autonomous agents cannot close tasks directly |
| QA review agent | `mark_task_review_approved` | Approves reviewed work, transitions toward close |

`mark_task_needs_review` and `mark_task_review_approved` are blocked in interactive sessions — use `close_task` directly. Conversely, autonomous agents must use the review flow.

## Closing Without Commits

For tasks that don't require code changes:

```python
call_tool("gobby-tasks", "close_task", {
    "task_id": "#N",
    "reason": "duplicate",  # or obsolete, wont_fix, already_implemented, out_of_repo
    "changes_summary": "Why no changes were needed"
})
```

Gates 3-4 still apply. `changes_summary` is still required — explain why no changes were needed.

## Common Mistakes

| Mistake | Why it fails | Fix |
|---------|-------------|-----|
| Close before commit | Gate 2 blocks — no commit linked | Commit first, pass `commit_sha` |
| `git add -A` | May stage secrets or binaries | Stage specific files |
| File task instead of fixing error | Gate 3 blocks — `errors_resolved` not set | Investigate and fix first |
| Skip memory review | Gate 4 blocks — `memory_review_completed` not set | Review or explicitly clear |
| Omit `changes_summary` | close_task rejects — required for leaf tasks | Describe what changed and why |
| Use mark_task_* in interactive session | Blocked by rule | Use `close_task` — user is the reviewer |
