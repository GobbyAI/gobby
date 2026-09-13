# Merge campaigns

Load when surveying unmerged work, resolving conflicts or landing local branches.
This guidance does not authorize a merge or worker launch by displaying choices.

Discover `gobby-merge:analyze_merge_landscape`, `predict_conflicts` and
`inspect_merge_state`. Confirm the user's target and selected workspaces.
Symmetric divergence is not the same as commits ahead. An empty survey requires
no worker; task no-work closure follows the tasks reference.

The survey includes at most 200 active/merged worktree records, not clones or
every repository branch. Missing paths and unknown ahead/behind counts require
inspection; an empty or limited survey is not proof that all work is delivered.
Conflict prediction reports missing workspace errors separately from pairwise
results. `inspect_merge_state` can hydrate missing conflict records; its `clean`
state means no merge/cherry-pick/rebase marker, not a clean working tree.

1. Survey all selected workspaces and predict both pairwise and target conflicts.
2. Plan recovery first, then task dependencies, fewer target conflicts, smaller
   diffs and freshness. Record each workspace, action and focused verification
   command. Preserve isolated test guards; never substitute a full test suite.
3. Dispatch the installed `merge-worker` for one workspace, or coordinate a
   campaign with `merge-orchestrator`. Inspect their installed definitions first.
   Put target/source and verification instructions in the prompt; use actual
   `spawn_agent` fields such as `task_id`, `worktree_id` or `clone_id`. There are
   no `assigned_task_id`, `target_branch` or arbitrary variable-bag spawn fields.
4. Workers use `merge_worktree`/`merge_clone` for final landing. For active
   worktree resolutions, inspect `merge_status`, resolve one `conflict_id` at a
   time with `merge_resolve`, recheck status, then `merge_apply`. Managed merge
   workers use their allowed MCP surface and do not synthesize manual contents
   through file-reading bypasses. Apply finishes source-worktree resolution;
   final landing still uses `merge_worktree` and its returned target SHA.
5. Subscribe once with `wait_for_agent` and yield. Process the delivered terminal
   result before replacement dispatch; complete paginated reports before judging.
6. Verify with the scoped `verify_in_worktree` command, then persist each result
   and report through `gobby-tasks-ops:record_merge_result`. This is execution
   verification; it does not manufacture transcript-derived leaf-close evidence.

For an explicitly expected-failing TDD test-writing deliverable, omit the green
`verify_command` gate. Cite its existing QA red-phase evidence and verify clean
landing instead; do not retry or escalate merely because the intended red test
fails. Other deliverables retain their applicable passing verification gate.

If the survey returns an active_resolution_id, continue that active resolution;
do not abort solely because a previous campaign recorded no progress. The
no-progress redispatch cap applies only after the current orchestrator run
completes a worker attempt and verifies that the resolution did not advance.
Keep live resolutions resumable. A busy-resolution response requires sequential
retry after inspection, not parallel conflict calls. `merge_apply` rejects pending
conflicts and marker-bearing/missing contents. `merge_abort` aborts an active Git
merge before deleting the resolution; failed abort preserves recovery evidence.
Rebases and exhausted resolver retries follow the campaign's escalation policy.

Use `cherry_pick_into_worktree` or `merge_subset` only for an explicitly selected
commit/path plan. Inspect resulting conflicts and index state; neither operation
by itself proves final target delivery. A cherry-pick continuation is a separate
Git operation, and a restricted worker needs an allowed recovery path.
`merge_subset` checks out source versions of paths and commits the current index;
ensure that index contains no unrelated staged changes before calling it.
It is a path snapshot commit, not a merge preserving source ancestry.

Operator `gobby merge start` creates or selects a resolution record; it does not
run the MCP resolver. CLI human resolve leaves the conflict pending, while JSON
output reports its actual status. Use the explicit MCP IDs for agent workflows.

After each verified landing, reconcile landscape, target SHA and workspace rows.
For worktree cleanup/timeout faults, inspect `landing_state` and
`cleanup_warnings`. `landed` retains verified target evidence; `unknown` requires
Git inspection before retrying. Preserve `retained_stash_oid` and restore only
the operation's stash, never whichever stash happens to be newest. A returned
schema cutover advisory is a separate operator procedure, not an executed restart.
Record failures/unresolved work, and perform [cleanup](cleanup.md). Read
[merge resolution tools](../../../../../../../../docs/guides/worktrees.md#merge-resolution-tools).
