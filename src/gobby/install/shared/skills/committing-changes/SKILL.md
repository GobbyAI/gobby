---
name: committing-changes
description: Commit message format and task close workflow. Use when ready to commit or closing tasks.
category: core
triggers: commit, git commit, commit changes, close task, close
metadata:
  gobby:
    audience: all
    format_overrides:
      autonomous: full
---

# Committing Changes - Commit and Close Workflow

This skill covers the commit message format and how to properly close tasks.

## Commit and Close Workflow

### Step 1: Stage Changes

```bash
git add <specific-files>
```

Prefer staging specific files over `git add -A`.

### Step 2: Commit with Task ID

```bash
git commit -m "[project-#N] type: description"
```

Use the `project-#N` format (e.g., `[gobby-#123]`) — the hyphen before `#` is required.

### Step 3: Close the Task

**If your agent instructions specify a different post-commit procedure** (e.g., `mark_task_needs_review` instead of `close_task`), **follow your agent instructions** — they take priority over this skill.

Otherwise, for standard sessions:

```python
call_tool("gobby-tasks", "close_task", {
    "task_id": "<task-id>",
    "commit_sha": "<commit-sha>"
})
```

## Commit Message Format

```
[<project>-#<N>] <type>: <description>

<optional body>
```

### Valid Commit Types

| Type | Use For |
|------|---------|
| `feat` | New feature |
| `fix` | Bug fix |
| `refactor` | Code restructuring |
| `test` | Adding tests |
| `docs` | Documentation |
| `chore` | Maintenance |

### Examples

```
[gobby-#123] feat: add user authentication
[gobby-#789] fix: resolve password reset bug
[gobby-#456] refactor: extract auth logic to service
[gobby-#12] test: add unit tests for auth module
```

## Closing Without Commits

For tasks that don't require code changes (research, planning, obsolete tasks):

```python
call_tool("gobby-tasks", "close_task", {
    "task_id": "<task-id>",
    "reason": "obsolete"  # or "already_implemented", "duplicate", "wont_fix", "out_of_repo"
})
```

## Common Mistakes

### Wrong: Close Before Commit

```python
# This will fail
call_tool("gobby-tasks", "close_task", {"task_id": "abc"})
```

### Right: Commit First, Then Close

```bash
git commit -m "[gobby-#42] feat: implement feature"
```

```python
call_tool("gobby-tasks", "close_task", {
    "task_id": "#42",
    "commit_sha": "a1b2c3d"
})
```

## Task Lifecycle

```
open → in_progress → review → closed
```

Tasks may enter `review` status instead of `closed` when:
- Task has `requires_user_review=true`
- Validation criteria fail

User must explicitly close reviewed tasks.
