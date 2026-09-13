# Commits

Load before staging, committing or preparing task-close evidence.

Discover the task's ownership, changed files and required verification first.
Use the [tasks implementation](../tasks/implementation.md) and
[closing](../tasks/closing.md) references for lifecycle gates. Load the installed
standalone `code-review` skill before the pre-commit review.

1. Inspect the current diff and attribution; keep foreign staged changes intact.
2. Finish edits and formatting, then run the required focused checks. Preserve
   isolated test-state guards. A later edit makes earlier close validation stale.
3. Stage specific owned paths and review their staged hunks.
4. Commit only those paths with the real project/task reference:

   ```bash
   git add path/to/owned-file
   git commit --only -m '[<project_name>-#<task_number>] fix: describe the change' -- path/to/owned-file
   ```

5. Keep the returned commit SHA for the task's lifecycle transition. Follow the
   close or autonomous stage workflow; a commit alone does not close work.

Use `feat`, `fix`, `refactor`, `test`, `docs` or `chore` as appropriate to the
repository convention. Replace example project/task names with the actual ones.
Never commit credentials, unrelated staged files or an unverified claim of
completion. A failed commit hook is a finding to fix and verify before retrying.

For isolated work, continue through [merge campaigns](merge-campaigns.md) or
[PR delivery](pr-delivery.md), then [cleanup](cleanup.md). Do not leave a managed
worktree registered as active after a manual landing.

See the [task guide](../../../../../../../../docs/guides/tasks.md) for detailed
commit attribution and close evidence.
