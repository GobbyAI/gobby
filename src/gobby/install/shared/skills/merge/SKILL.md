---
name: merge
description: "Launch a merge campaign. Surveys unmerged worktrees and routes to merge-worker (single) or merge-orchestrator (multi)."
version: "1.0.0"
category: core
triggers: merge, merge campaign, land worktrees, merge worktrees
metadata:
  gobby:
    audience: interactive
    depth: 0
---

<!-- markdownlint-disable MD013 -->

# /gobby merge

You have been invoked as `/gobby merge`. Survey what is unmerged, then
route the user to the right merge flow.

## Step 1: Survey

Call `gobby-merge:analyze_merge_landscape` (no args, or pass
`project_id` if the user named one). The result is a list of
`{worktree_id, branch, base, divergence_commits, files_touched,
last_commit_at, merge_state, task_ref}`.
`divergence_commits` is symmetric divergence (`base..HEAD` plus `HEAD..base`),
not just commits ahead.

Then call `gobby-merge:predict_conflicts` with the surveyed
`worktree_ids` and the user's target branch (default `main`). Use the
`target_predictions` array to label each worktree clean vs. conflicting.

## Step 2: Route by count

### 0 unmerged worktrees

Tell the user there is nothing to merge. Do not spawn anything. Stop.

### 1 unmerged worktree

Show the user the single candidate (branch, files touched, conflict
prediction). Ask for a one-shot confirmation:

> "Merge `<branch>` into `<target>`? (this dispatches a merge-worker)"

On confirmation, spawn the worker via `gobby-agents:spawn_agent`:

```python
call_tool("gobby-agents", "spawn_agent", {
    "agent": "merge-worker",
    "prompt": "Merge worktree <worktree_id> into local <target_branch>. Call merge_worktree with target_branch=<target_branch>, record its merge_sha as the final target merge SHA, and do not push; remote publication is PR-only.",
    "worktree_id": "<worktree_id>",
    "target_branch": "<target_branch>",
    "isolation": "none",
})
```

Surface the worker's `run_id` and `session_id` so the user can follow
along. Do not poll inside the skill — the worker self-terminates via
`end_agent_run`. `kill_agent` is for external targeted termination, not
normal self-completion.

### 2+ unmerged worktrees

This is a campaign. Show the user the survey: a numbered list of
worktrees with conflict predictions, and a one-line summary
("3 clean against `main`, 2 predicted conflicts").

Ask for confirmation:

> "Run a merge campaign across N worktrees into `<target>`? The
> orchestrator will plan an order, dispatch one merge-worker per step,
> and verify after each."

On confirmation:

1. **Create a campaign task.** Call `gobby-tasks:create_task` with:
   - `title`: `"Merge campaign: N worktrees into <target>"`
   - `category`: `"code"`
   - `task_type`: `"task"`
   - `validation_criteria`: name the worktree IDs, the target branch,
     and require a delivery campaign report recorded through
     `gobby-tasks-ops:record_merge_result`.
   - `claim`: `false` — the orchestrator will claim it itself.

2. **Spawn the orchestrator** via `gobby-agents:spawn_agent`:

```python
call_tool("gobby-agents", "spawn_agent", {
    "agent": "merge-orchestrator",
    "prompt": "Run the merge campaign for task <campaign_task_ref>. Survey, plan, dispatch merge-workers, verify, and write the campaign report.",
    "assigned_task_id": "<campaign_task_ref>",
    "target_branch": "<target_branch>",
    "isolation": "none",
})
```

Surface the campaign task ref and the orchestrator's `run_id` /
`session_id`.

## Optional arguments

The user may pass arguments after `/gobby merge`:

- `/gobby merge` → survey + route as above.
- `/gobby merge <worktree_id>` → skip the survey count branch and go
  straight to the single-worker flow for that worktree.
- `/gobby merge --target <branch>` → override the default target branch
  (`main`).
- `/gobby merge --yolo` → set `yolo=true` on the campaign task so the
  orchestrator force-advances on stuck steps instead of escalating.

If no arguments are passed, ask one clarifying question only when
ambiguous; otherwise default to `main`.

## Boundaries

- Do not run `git merge` directly. Routing only.
- Do not call `merge_resolve` or `merge_apply` from the skill — those
  belong to the worker.
- Do not poll for completion inside the skill. Surface IDs and stop.
