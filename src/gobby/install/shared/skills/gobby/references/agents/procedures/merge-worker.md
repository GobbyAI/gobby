# merge-worker procedures

Detecting your isolation type:
- Check your session variables (set during spawn)
- If you have worktree_id: use worktree merge flow
- If you have clone_id: use clone merge flow

Worktree merge flow:
MAX_RESOLVE_RETRIES: 3 per conflict id.
1. If the spawn prompt asks for preflight or branch metadata, stay inside
   the merge step MCP surface — Bash/Read metadata bypasses merge tracking.
   Then call merge_worktree with worktree_id and target_branch. Do not push.
   merge_worktree is the authoritative final landing tool for worktree
   delivery. Its merge_sha/target_head_sha is the final local target branch
   SHA to record. Do not use Bash, Read, or merge_start/merge_apply to
   discover or substitute the final merge SHA.
2. If SUCCESS: when the spawn prompt provides a verification command, call
   gobby-merge:verify_in_worktree with the worktree_id and that command
   before recording success. If verification fails, call
   gobby-tasks-ops:record_merge_result with failure_reason. If verification
   passes or no verification command was supplied, call
   gobby-tasks-ops:record_merge_result with the merge_sha returned by
   merge_worktree. The
   workflow will transition to cleanup (delete your worktree), then
   terminate; call end_agent_run only after the terminate transition.
3. If CONFLICTS (has_conflicts: true):
   a. Call merge_start with worktree_id and the source/target branches
      where source_branch is the worktree branch and target_branch is the
      local branch being merged into. Never pass the target branch as
      source_branch to "pull target into the worktree"; merge_start already
      updates the worktree branch from target before applying resolutions.
      (this resets merge_resolve_attempts=[] for deterministic retry enforcement)
   b. Check merge_status for conflict details
   c. Resolve conflicts sequentially. Call merge_resolve with use_ai=true
      for exactly one conflict_id at a time, then call merge_status before
      attempting the next pending conflict. Never issue multiple
      merge_resolve calls in the same assistant turn or parallel tool batch.
      Track attempts by conflict_id. If one conflict reaches
      MAX_RESOLVE_RETRIES, skip that conflict_id and continue with the
      remaining conflicts.
   d. Re-check merge_status after each attempt and before merge_apply. Call
      merge_apply only if all conflicts are resolved. merge_apply updates
      the source/worktree branch for the resolved content; it is not the
      final target-branch delivery SHA.
   e. Call merge_worktree exactly once with target_branch to merge the
      resolved work locally. Record the merge_sha returned by merge_worktree;
      never record the merge_apply SHA as the final delivery SHA.
   f. Call gobby-tasks-ops:record_merge_result with that merge_sha. The workflow will
      transition to cleanup (delete your worktree), then terminate; call
      end_agent_run only after the terminate transition.
4. If any conflicts remain unresolved after all conflict_ids were attempted:
   record gobby-tasks-ops:record_merge_result with failure_reason including
   the unresolved conflict ids/files and retry counts. The workflow will
   transition to terminate; call end_agent_run only after that transition.
   Do not call merge_abort solely because one conflict_id reached its retry cap.

Clone merge flow:
1. Sync the clone first: call sync_clone with clone_id
2. Call merge_clone with clone_id and target_branch
3. Record gobby-tasks-ops:record_merge_result with merge_sha on success or
   failure_reason on conflicts/failure. The workflow then transitions
   to cleanup — clones have no worktree to delete, so
   send EVENT=CLONE_CLEANUP_DELEGATED NOTE= to your coordinator, then terminate.
   On failure it transitions straight to terminate. Call end_agent_run only
   after the terminate transition.

Lifecycle model:
- On successful merge, call gobby-tasks-ops:record_merge_result with the
  task id assigned for the merged work and merge_sha before end_agent_run.
- When the spawn prompt provides a verification command, verification is a
  pre-record success gate. Run gobby-merge:verify_in_worktree after
  merge_worktree succeeds and before record_merge_result.
- Pass verification commands exactly as supplied. Commands may include
  required environment guards such as `GOBBY_TEST_PROTECT=1 uv run pytest ...`
  or `env GOBBY_TEST_PROTECT=1 uv run pytest ...`; do not strip those guards
  or retry pytest without them.
- On unresolved conflict or verification failure, call
  gobby-tasks-ops:record_merge_result with failure_reason before end_agent_run.
- end_agent_run belongs after the merge step: first record a successful
  or failed merge result. Those
  MCP calls move the workflow onward: successful merges go to the cleanup
  step — delete the merged worktree with gobby-worktrees:delete_worktree
  (default flags); never leave a merged worktree registered active — and
  failures go straight to terminate. end_agent_run belongs in terminate.
- Rules are authored against semantic workflow events such as `turn_start` and `turn_end`.
- Raw `before_agent`, `after_agent`, and `stop` are provider/runtime details, not the main authoring API.
- End-of-turn gates may keep the current turn alive until required merge work is complete.
- Merge completion uses `end_agent_run`; `kill_agent` is for external targeted termination.
