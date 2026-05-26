---
name: source-control
description: Commit message format and release PR process. Use when ready to commit or push a release; use task-transitions for task close/review gates.
category: core
triggers: commit, git commit, commit changes, release, push release, create pr, pull request
metadata:
  gobby:
    audience: all
    format_overrides:
      autonomous: full
---

# Source Control - Commits, Closes, and Releases

This skill covers commit message format and the release PR process.

Use the `task-transitions` skill for task lifecycle transitions such as `close_task`,
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

Use the real project name in the task reference (e.g., `[gobby-#123]` or
`[gobby-cli-#123]`). `project_name` is a placeholder, never a literal prefix.
The hyphen before `#` is required.

### Step 3: Task Transitions (close_task, review, validation)

After committing, follow the `task-transitions` skill for the correct task lifecycle
action (`close_task`, `submit_for_review`, review approval, validation gates, and
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
[gobby-#123] feat: add user authentication
[gobby-#789] fix: resolve password reset bug
[gobby-#456] refactor: extract auth logic to service
[gobby-#12] test: add unit tests for auth module
```

## Common Mistakes

### Wrong: Commit Without Task Reference

```bash
git commit -m "fix: implement feature"
```

### Right: Include the Task in the Commit Message

```bash
git commit -m "[gobby-#42] feat: implement feature"
```

---

## Part 2: Release PR Workflow

When you're ready to cut a release from a working branch (e.g., `0.3.1`):

### Step 1: Version Bump

Update all version files on the working branch:

1. `pyproject.toml` — `version` field
2. `src/gobby/__init__.py` — `__version__` variable
3. `CHANGELOG.md` — add new `[version]` section
4. Run `uv sync` to update `uv.lock`

Commit: `[gobby-#N] chore: bump version to X.Y.Z`

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
