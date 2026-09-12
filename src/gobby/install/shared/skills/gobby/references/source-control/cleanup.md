# Cleanup

Load before releasing, abandoning, deleting or recovering managed workspaces.

Discover the exact worktree/clone row and owner. Use `detect_stale_worktrees` or
`detect_stale_clones` for discovery, then preview cleanup with `dry_run=true`.
Age and metadata status alone are not proof that files or commits are disposable.

1. For normal worktree delivery, call `merge_worktree`, retain its landing SHA,
   then `delete_worktree` with default safety flags. After a manual landing,
   reconcile with `mark_worktree_merged` before deletion when its base proof holds.
2. If the stored base has been removed, an explicit `merged_into` must name the
   actual local landing branch containing the source tip. Remote refs, tags,
   revision expressions and self-containment do not authorize deletion.
3. `force` controls dirty-file removal; `force_delete_branch` abandons unmerged
   commits. They are distinct and neither belongs to normal landing. Preserve
   tracked changes; the agent's exceptional force path is limited to verified
   disposable untracked-only dirt. `merged_into` cannot combine with forced
   branch deletion.
4. Use `release_worktree`/`release_clone` for an explicit ownership handoff.
   Abandoning metadata does not delete content. Never manually remove a managed
   Git worktree/branch and leave its database record behind.
5. Worktree stale cleanup marks inactive active rows abandoned; optional Git
   deletion checks dirt/merge state. Expired merged rows are also considered.
   Inspect each result's skipped/error fields even when top-level success is true.
6. Clone stale cleanup can mark rows stale, and optional file deletion is forced:
   authorize disposal of the selected content before `delete_files=true`.
   Ordinary `delete_clone` checks dirt unless forced; verify landing and valuable
   commits independently. File deletion and row deletion can fail separately.

Deletion by path can adopt an existing checkout before removing it. Inspect the
path, machine and project identity before using that recovery surface; never
adopt an unrelated repository just to bypass missing registration or ownership.

On partial failure, retain row/path/error evidence and repair the reported step.
Re-query the final row state; a finished task must not leave an active managed
workspace unless explicitly handed off. See
[deleting after landing](../../../../../../../../docs/guides/worktrees.md#deleting-after-final-landing)
and [stale cleanup](../../../../../../../../docs/guides/worktrees.md#stale-cleanup).
