---
name: source-control
description: Commit message format, task-branch landing with worktree cleanup, and release PR process. Use when ready to commit, land a task branch, or push a release; use tasks for task close/review gates.
version: "1.1.0"
category: core
triggers: commit, git commit, commit changes, merge, land branch, merge worktree, delete worktree, release, push release, create pr, pull request
metadata:
  gobby:
    audience: all
    format_overrides:
      autonomous: full
---

# Source Control - Commits, Closes, and Releases

This skill covers commit message format and the release PR process.

Use the `tasks` skill for task lifecycle transitions such as `close_task`,
`submit_for_review`, validation gates, and commit SHA requirements.

---

## Part 1: Commit Workflow

### Step 1: Stage Changes

```bash
git add <specific-files>
```

Prefer staging specific files over `git add -A`.

### Step 2: Commit with Task ID

```bash
git commit -m "[<project_name>-#<task_number>] <type>: <description>"
```

Template examples use `[<project_name>-#<task_number>]`; replace both
placeholders with the real project name and task number before committing.
The hyphen before `#` is required.

### Step 3: Task Transitions (conditional close, review, validation)

After committing, follow the `tasks` skill for the correct task lifecycle
action (conditional `close_task`, `submit_for_review`, review approval, validation gates, and
memory review).

## Commit Message Format

```
[<project_name>-#<task_number>] <type>: <description>

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
[<project_name>-#123] feat: add user authentication
[<project_name>-#789] fix: resolve password reset bug
[<project_name>-#456] refactor: extract auth logic to service
[<project_name>-#12] test: add unit tests for auth module
```

## Common Mistakes

### Wrong: Commit Without Task Reference

```bash
git commit -m "fix: implement feature"
```

### Right: Include the Task in the Commit Message

```bash
git commit -m "[<project_name>-#42] feat: implement feature"
```

---

## Part 2: Landing a Task Branch (Worktrees)

A task branch that lives in a Gobby-managed worktree
(`~/.gobby/worktrees/<project>/task-NNNNN-*`) must be landed **through the
worktree tools**, never with a bare `git merge`. A manual merge leaves the
worktree's DB record `active` forever: `mark_merged` never runs,
`cleanup_after` is never stamped, and the daemon reaper can never reclaim the
worktree. The worktree, its branch, and its registration all leak.

### Landing (the normal path)

```python
call_tool("gobby-worktrees", "merge_worktree", {"worktree_id": "<id>"})
call_tool("gobby-worktrees", "delete_worktree", {"worktree_id": "<id>"})
```

Use default deletion flags (`force=false`, `force_delete_branch=false`).
`delete_worktree` refuses dirty trees and unmerged branches — if it refuses,
resolve the reason; do not reach for force flags.

### If a manual `git merge` already happened

Repair the record immediately, then delete:

```python
call_tool("gobby-worktrees", "mark_worktree_merged", {"worktree_id": "<id>"})
call_tool("gobby-worktrees", "delete_worktree", {"worktree_id": "<id>"})
```

### Rules

- Never run `git worktree remove` or `git branch -D` by hand on a managed
  worktree — the DB record only updates through the tools.
- `force=true` is only for verified untracked-only dirt.
  `force_delete_branch=true` abandons unmerged commits and is never part of a
  normal landing.
- Not landing yet (handing the worktree to another session or agent)? Use
  `release_worktree` — never leave a finished task's worktree `active`.
- After landing, `list_worktrees` must show no `active` row for the task.

---

## Part 3: Release PR Workflow

When you're ready to cut a release from a working branch (e.g., `0.5.0`):

### Step 1: Version Bump

Update all version files on the working branch:

1. `pyproject.toml` — `version` field
2. `src/gobby/__init__.py` — `__version__` variable
3. `CHANGELOG.md` — add new `[version]` section
4. Run `uv sync` to update `uv.lock`

Commit: `[<project_name>-#N] chore: bump version to X.Y.Z`

### Step 2: Push and Create PR

```bash
git push origin <branch>
gh pr create --base main --head <branch> --title "Release vX.Y.Z"
```

This triggers the `claude-code-review.yml` workflow — Claude reviews the PR automatically.

### Step 3: Address Review Feedback

Fix anything flagged by the Claude review, push updates. The review re-runs on `synchronize`.

### Step 4: Merge and Tag

```bash
# Merge the PR (via GitHub UI or CLI)
gh pr merge <number> --merge

# Tag from main
git checkout main && git pull
git tag vX.Y.Z
git push origin vX.Y.Z
```

The `v*` tag triggers the release workflow: test → build → PyPI publish → GitHub Release.

### Step 5: Start Next Version

```bash
git checkout -b X.Y.(Z+1)
# Bump version files to next patch
# Commit and push
```

### Release Checklist

- [ ] Version files updated (pyproject.toml, __init__.py, CHANGELOG.md, uv.lock)
- [ ] PR created to `main`
- [ ] Claude review passed
- [ ] PR merged
- [ ] Tag pushed (`vX.Y.Z`)
- [ ] Release workflow completed (check GitHub Actions)
- [ ] Next version branch created and bumped
