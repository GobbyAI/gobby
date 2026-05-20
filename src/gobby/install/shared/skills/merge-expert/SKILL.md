---
name: merge-expert
description: Methodology for PR-aware delivery — probe branch protection, open/gate PRs, land via GitHub when required, or run the direct multi-worktree merge campaign.
version: "1.0.0"
category: core
internal: true
triggers: merge campaign, multi-worktree merge, merge orchestrator, surgical merge
metadata:
  gobby:
    audience: all
    depth: 0
---

# merge-expert — Gobby PR/Merge Delivery Methodology

> Internal methodology skill; loaded with `get_skill(name="merge-expert")` by
> the `merge-orchestrator` agent before it handles either the `pr` or `merge`
> stage.

Use this skill to drive bounded delivery. In the `pr` stage, decide whether
the target branch requires a GitHub PR, open or update the PR, gate on CI and
external review, and record the canonical PR verdict. In the `merge` stage,
land approved PRs via GitHub or run the existing direct merge campaign for
unprotected branches.

## Inputs

A campaign starts with a single coordinating task. Read its description,
labels, and `task_artifacts` first. Useful inputs:

- A list of worktree IDs (or "all unmerged in this project") from the
  campaign task description.
- The target branch (default `main` unless the task overrides it).
- A verification command per worktree, if the task specifies one. Otherwise
  default to whatever the project conventionally runs (`uv run pytest`, `npm
  test`, etc.); if you are unsure, skip the gate and note that in the report.
- Existing delivery state from `gobby-tasks-ops:get_delivery_state`.
- `yolo` flag from task state. Under yolo, skip PR only when the branch probe
  says no protection exists; never bypass protected-branch review gates with
  bot self-approval.

## PR Stage

For each delivery unit:

1. Call `gobby-merge:probe_branch_protection` for the target branch and
   persist the result with `gobby-tasks-ops:record_pr_state`.
2. If `requires_pr=false`, record `pr_required=false`, `pr_state=direct_merge`,
   submit the `pr` stage for review, then call
   `record_pr_verdict(verdict="approve", ...)`. The merge stage will run the
   direct merge-worker path.
3. If `requires_pr=true`, push the source branch with
   `gobby-worktrees:push_branch`, create or reuse a GitHub PR, and call
   `record_pr_opened`. Post holistic QA notes to the PR with
   `github:create_pull_request_review(event="COMMENT")`; never use APPROVE.
4. Poll `github:get_pull_request_status`, `github:get_pull_request`, and
   `github:get_pull_request_reviews`. Persist each gate snapshot with
   `record_pr_state`. If waiting on CI or humans, end the agent run; the
   dispatcher heartbeat will resume later.
5. If the PR becomes conflicting, first try
   `github:update_pull_request_branch`. If that cannot update the branch,
   perform one local AI-assisted update using `gobby-merge:merge_start`,
   `merge_resolve`, and `merge_apply`, then `gobby-worktrees:push_branch` with
   `force_with_lease=true`. Record `local_update_attempts=1`. A second conflict
   event escalates.
6. When CI, review, and mergeability are ready, call
   `record_pr_verdict(verdict="approve", findings=...)`. Use
   `request_changes` for concrete blockers and `needs_discussion` only when a
   human decision is required.

## Methodology

Run the direct merge campaign in five phases. Do not skip phases; do not
collapse them into a single LLM step.

### 1. Survey

Build a complete picture before deciding anything. Call:

- `gobby-merge:analyze_merge_landscape` — list every unmerged worktree with
  its branch, base, divergence count, files touched, last commit time, and
  originating task ref.
- `gobby-merge:predict_conflicts` — pairwise + per-target `git merge-tree`
  simulation. Lists which worktrees will conflict with each other and which
  will conflict with the target branch.
- `gobby-merge:inspect_merge_state` for any worktree whose `merge_state` is
  non-null in the landscape — there may be orphaned `MERGE_HEAD` /
  `CHERRY_PICK_HEAD` / rebase state to recover before scheduling new work.

Refuse to plan if the landscape is empty (`worktrees == []`). Close the
campaign task with `reason=already_implemented` instead.

### 2. Plan

Produce an ordered merge plan. The ordering rubric, in priority order:

1. **Recover orphans first.** Any worktree returned by `inspect_merge_state`
   with `state != "clean"` and `can_resume=true` must be recovered before
   scheduling fresh merges in the same worktree. If it exposes an
   `active_resolution_id` with pending conflicts, continue that active
   resolution with `merge_status` plus a `merge-worker`; do not abort solely
   because a previous campaign recorded no progress.
2. **Toposort by task dependencies.** If the campaign worktrees correspond to
   tasks with `blocked_by` relations, predecessors merge before dependents.
3. **Fewest predicted conflicts against the target.** Worktrees with `clean:
   true` from `predict_conflicts.target_predictions` go first — they merge
   without resolution and reduce the conflict surface for the rest.
4. **Smallest diff next.** Among the still-conflicting worktrees, prefer the
   smaller `divergence_commits` × `len(files_touched)` because a smaller
   symmetric change is easier for the AI resolver and easier to verify.
5. **Freshness as a tiebreaker.** Newer `last_commit_at` ahead of older when
   everything else is equal — newer code reflects current intent better.

For each step, decide an action:

- `merge` — full `merge-worker` flow (default). Use whenever the worktree's
  branch should land in the target wholesale.
- `cherry-pick` — when the worktree contains commits that belong on a
  different base than its current branch (rare, but happens when a worktree
  was forked from the wrong base). Drives `cherry_pick_into_worktree`.
- `merge_subset` — when only specific paths from the worktree should land
  (the rest is exploratory or stale). Drives the `merge_subset` tool.
- `abort` — when `inspect_merge_state` shows orphaned state that cannot be
  resumed cleanly, has no active resolution to continue, or when the worktree
  is purely abandoned work. Drives `merge_abort` then `delete_worktree`.

Emit the plan as a structured list into a session variable
`merge_plan` so subsequent phases can iterate it:

```yaml
- step_no: 1
  worktree_id: wt-abc
  action: merge
  expected_conflicts: []
  verify_command: "uv run pytest tests/storage/"
- step_no: 2
  worktree_id: wt-def
  action: cherry-pick
  commits: ["a1b2c3d"]
  verify_command: "uv run pytest tests/clones/"
```

If the plan is empty after filtering aborts, close the campaign task with
`reason=already_implemented`.

### 3. Execute

Iterate the plan in order. For each step:

1. **Pre-flight.** Re-run `inspect_merge_state` on the target worktree. If
   it is dirty unexpectedly, abort the step and replan.
2. **Dispatch.** Call `gobby-agents:spawn_agent` (or `dispatch_batch` for a
   group of independent `clean: true` steps) with `agent=merge-worker` and
   pass the `worktree_id`, `target_branch`, and source-branch info as
   spawn-time variables. `source_branch` is always the worktree branch; never
   use the target branch as source to "pull latest target" into the worktree.
   For a clean direct merge, instruct the worker to call
   `gobby-worktrees:merge_worktree`; if the plan has a `verify_command`, include
   it in the worker prompt and require `gobby-merge:verify_in_worktree` before
   the worker records success. The worker records the `merge_sha` returned by
   `merge_worktree` as the final target branch SHA only after verification
   passes, or immediately when no verification command was supplied. Do not ask
   a clean worker to use `merge_start`/`merge_apply` as the landing path;
   `merge_apply` resolves the source/worktree branch and is not the final target
   merge SHA. The worker handles the actual merge + AI resolution. When dispatching a
   worker to continue an active resolution, keep the prompt inside the
   merge-worker MCP surface: tell it to call `merge_status`, then
   resolve exactly one pending conflict_id at a time with
   `merge_resolve(conflict_id=..., use_ai=true)`, call `merge_status` again,
   and only then continue to the next pending conflict. Once `merge_apply`
   completes, tell it to call `merge_worktree` exactly once and record
   `merge_worktree`'s `merge_sha`; never record the `merge_apply` SHA as the
   final delivery SHA. Tell it not to issue multiple `merge_resolve` calls in
   the same assistant turn or parallel tool batch. If retries/timeouts are
   exhausted, tell it to call
   `gobby-tasks-ops:record_merge_result` with `failure_reason` including the
   unresolved ids/files, then terminate through its normal `end_agent_run`
   step. Never ask the worker to use Read/Bash or synthesize manual `resolved_content`
   from file inspection.
3. **Wait for the worker.** Workers terminate themselves via `end_agent_run`;
   call `gobby-agents:wait_for_agent(run_id=...)` to wait for completion, then
   call `gobby-agents:get_agent_result` only if you need to re-read the final
   report. Do not use Bash sleep loops, tmux polling loops, or provider Monitor
   for worker waits.
4. **Verify.** Prefer worker-side verification: pass the step's
   `verify_command` to the merge-worker and require it to run
   `gobby-merge:verify_in_worktree` before `record_merge_result`. Treat exit
   code 0 as the gate. If a legacy worker did not run verification, the
   orchestrator must run the same command with `verify_in_worktree` before
   reporting success. Capture stdout/stderr in the campaign report on failure.
5. **Decide retry vs. escalate.**
   - Worker reports success + verify passes → mark step complete, move on.
   - Worker reports success + verify fails → re-dispatch the worker with the
     verify failure context once. If it still fails, escalate the step.
   - Worker reports failure with `needs_human_review=true` → escalate the
     step (or under `yolo`, mark the step as `force-advanced` and continue).
   - Worker reports failure with conflicts the AI couldn't resolve →
     `needs_human:merge_resolution_failed:<file_list>`.

Append a `task_lifecycle_event` per step (`merge_step_started`,
`merge_step_completed`, `merge_step_failed`) so the audit trail is complete.

### 4. Recover

Whenever `inspect_merge_state` shows orphaned state during pre-flight, treat
it as a recovery action before any new dispatch:

- `state == "merging"` with conflicts and an `active_resolution_id` → call
  `merge_status`, then dispatch a `merge-worker` to continue the pending
  conflicts in that active resolution. The no-progress redispatch cap applies
  only after a worker from the current orchestrator run completes and the next
  `merge_status` is unchanged.
- `state == "merging"` with conflicts but no resumable active resolution →
  call `merge_abort`, then re-survey before scheduling a fresh `merge` step.
- `state == "cherry-picking"` with conflicts → call
  `gobby-merge:merge_resolve` on each conflict, then **dispatch a
  `merge-worker` to run `git cherry-pick --continue`** in the worktree (no
  MCP wrapper today; worker delegation is required because the orchestrator
  is read+dispatch only).
- `state == "rebasing"` → escalate. The orchestrator does not unwind in-
  progress rebases; that is a human decision.

Recovery actions land in the campaign report under `recoveries:`.

### 5. Verify

Per-step verification is in §3. After all steps complete, run a campaign-
level sanity check:

- `gobby-merge:analyze_merge_landscape` again. Every worktree the plan was
  meant to merge should now be `status=merged` (or absent from the list).
- `gobby-worktrees:list_worktrees` filtered to `status=active` should show
  only worktrees the campaign explicitly skipped.
- For each merged worktree that the campaign successfully landed, the target
  branch's `git log` should include the merge commit (`gobby-merge` writes
  the merge commit; verify via `verify_in_worktree` running `git log
  --grep`).

If any of these post-conditions fail, do not approve the campaign — report
what is missing.

## Output

Write a campaign report and pass its reference to
`gobby-tasks-ops:record_merge_result`. Required JSON shape:

```json
{
  "campaign_id": "<task ref>",
  "target_branch": "main",
  "plan": [ /* the merge_plan from phase 2 */ ],
  "executed_steps": [
    {
      "step_no": 1,
      "worktree_id": "wt-abc",
      "action": "merge",
      "outcome": "merged",
      "verify_exit_code": 0,
      "merge_commit": "<sha>",
      "duration_seconds": 47
    }
  ],
  "recoveries": [],
  "failures": [],
  "unresolved_worktrees": [],
  "summary": "<one-paragraph human summary>"
}
```

Then transition the campaign task:

- All steps merged + verified -> `gobby-tasks-ops:record_merge_result` with
  `merge_sha` and `report_ref`.
- Any step ended in `needs_human:` -> `escalate_task` with the failure list
  in the reason.
- Unresolved merge failures -> `gobby-tasks-ops:record_merge_result` with
  `failure_reason` and `report_ref`.

## Boundaries

- Do **not** call `merge_resolve` directly. That is `merge-worker`'s job;
  the orchestrator only routes except for the single allowed local PR update
  fallback after `github:update_pull_request_branch` fails.
- Do **not** spawn agents deeper than 5 levels — the `child_session_manager`
  enforces this, but plan around it: workers cannot spawn workers.
- Do **not** `close_task` on the campaign task. Use
  `gobby-tasks-ops:record_merge_result` or `escalate_task`.
- Do **not** edit code. The orchestrator is read + dispatch only.
