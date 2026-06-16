Coordinate parallel implementation of repo-wide review section epics under #15766 using gobby-agents developer workers.

Role: coordinator only. No review work. No merge work. No leaf implementation by the coordinator.

Authority:
Live MCP state is authoritative every loop. Use these surfaces directly:
gobby-tasks for task lifecycle, task trees, leaf inspection, and stage inspection.
gobby-agents for developer worker dispatch, active-run reconciliation, and terminal results.
gobby-worktrees for live worktree records.
gobby-memory for the live coordinator manifest.
gobby-sessions for compact_self only.

Startup bootstrap:
1. Reconcile live state before any dispatch.
2. Verify local branch `0.5.0` exists and is usable. If it is missing, stop and report the blocker.
3. Verify active agents through gobby-agents and live worktree records through gobby-worktrees.
4. Rebuild the queue from direct child epics of #15766. A section is a direct child epic titled `Review fixes: <slug>`.
5. Ignore closed non-section chores under #15766.
6. Rebuild the coordinator manifest from live MCP state before any dispatch.
7. Reset `dispatches_since_compact` to `0` after the bootstrap manifest rebuild.

Bootstrap seed data observed on 2026-06-16:
No active agents.
gobby-worktrees currently reports no worktree records.
Local `0.5.0` exists and is the current branch.
#15766 is open.
#16926 through #16934 have all child leaves closed but section epics remain open.
#16935 is closed.
#16936 and later still have open leaves; the current first eligible seed is #15883 under config.
Manifest memory `ebb7269b-7083-55ef-ad92-47a0b653c583` is stale. Rebuild it from live MCP state before dispatch. Do not trust worktree IDs, paths, active leaves, or run IDs from that stale content.

Prohibited actions:
Source file edits by the coordinator.
Leaf task implementation by the coordinator.
QA, reviewer, or merge agent spawning.
Review, merge, push, mark merged, delete, or abandon worktrees.
Closing #15766.
Resurrecting deleted `wt-*` IDs, stale worktree paths, or stale branch state from memory.
Creating worktrees for sections with no open leaves.
Developer dispatch outside worktree isolation.

Bounded waiting:
Do not use wait_for_agent as routine polling.
You may use gobby-agents.wait_for_agent only as the last idle action when agents are already running, no coordinator work remains, and the call is bounded with `timeout_seconds = 300`.

Base branch:
All section worktrees are based on local `0.5.0`.
Use `base_branch = "0.5.0"` for new section worktree dispatch.
Do not dispatch if local `0.5.0` is unavailable.

Queue:
Build the queue from direct child epics of #15766.
Each direct child epic is one section.
Do not dispatch into closed sections or sections with no open leaves.
For sections with open leaves, create or reuse one live worktree per active section.
Branch names: `review-fixes/<section-slug>`.
If no live worktree record exists for a section, treat that as normal current state and create a new worktree through spawn_agent on first dispatch.
If a live worktree record exists, verify it through gobby-worktrees before reusing it.

Coordinator manifest:
Maintain a live manifest memory with one section record per direct child epic. The existing memory ID is `ebb7269b-7083-55ef-ad92-47a0b653c583`; update that memory if possible. If the memory no longer exists or cannot be updated, create a replacement and record the new memory ID in the handoff.

Manifest fields per section:
section epic ref
section title
section slug
branch name
base branch
live worktree_id, if any
live worktree path, if any
active leaf ref, if any
active agent run_id, if any
closed leaf refs
blocked leaf refs with reasons
validation notes
status: queued, active, complete_no_active_worktree, ready_for_external_review, blocked

Manifest rules:
Live MCP state wins over manifest content every loop.
Completed sections with no open leaves must be marked `complete_no_active_worktree` when no live worktree exists, or `ready_for_external_review` when a live clean worktree/branch is available for review.
Never dispatch into a section marked `complete_no_active_worktree` or `ready_for_external_review` unless live MCP state later shows a newly open eligible leaf.
Keep #15766 open.

Concurrency:
Run at most 6 active developer workers total.
Run at most one active developer worker per section.
Count active workers from gobby-agents live state, not memory alone.

Leaf selection:
Dispatch one leaf task at a time per active section.
Select the next open, unclaimed, non-escalated, non-blocked leaf under the section in priority order: 1, then 2, then 4.
Skip closed leaves.
Record blocked leaves and continue with the next eligible leaf.

Developer agent selection:
Choose developer agent from leaf `implementation_domain`:
backend -> backend-developer
frontend -> frontend-developer
fullstack -> fullstack-developer
If `implementation_domain` is missing or unknown, record the leaf as blocked with the reason and continue.

Pre-dispatch checks:
Use gobby-agents.evaluate_spawn before every dispatch.
Do not pass `worktree_id` to evaluate_spawn; it does not accept that parameter.
Before dispatching a leaf, inspect get_task_stages.
Dispatch only leaves whose stage manifest allows developer self-close.
If a leaf has a development review gate or would require submit_for_review, record it as blocked_by_review_gate and continue.

First dispatch for a section with no live worktree:
Call gobby-agents.spawn_agent with:
agent = selected developer
task_id = leaf task ref
isolation = "worktree"
branch_name = "review-fixes/<section-slug>"
base_branch = "0.5.0"
notify_parent_on_completion = true
prompt = the developer worker prompt

Record the returned `worktree_id`, worktree path, run_id, active leaf, and branch in the manifest.

Dispatch for a section with a verified live worktree:
Call gobby-agents.spawn_agent with:
agent = selected developer
task_id = leaf task ref
worktree_id = verified section worktree id
isolation = "worktree"
notify_parent_on_completion = true
prompt = the developer worker prompt

Do not use stale or unverified worktree IDs.
If a spawn call is interrupted, reconcile with list_agent_runs before dispatching again.

Developer worker requirements:
Claim the assigned leaf before editing.
Work only on the assigned leaf.
Commit with `[gobby-#NNNNN] ...`.
Run focused validation for touched code.
Close the leaf with close_task and `commit_sha`.
Call end_agent_run before exiting.
Escalate blockers through gobby-tasks.

Coordinator loop:
On daemon activity, call deliver_pending_messages(target_session_id=<parent session>).
Reconcile active and terminal runs with list_agent_runs(parent_session_id=<parent session>).
Use get_agent_result only for terminal runs.
When a worker finishes, refresh that task and its section's live worktree record.
If the task closed valid and the worktree is clean, update the manifest and dispatch the next eligible leaf in the same section.
If a worker fails with provider stall, inspect the task and live worktree state. If a verified live worktree is dirty, retry by continuing in that same worktree. If no verified live worktree exists, rebuild from live task state and dispatch normally.
If all leaves under a section are closed or blocked, record section status `complete_no_active_worktree`, `ready_for_external_review`, or `blocked` as appropriate.
Do not dispatch into already completed sections just because their section epic remains open.
Keep #15766 open.

Compaction:
Maintain `dispatches_since_compact`.
Increment after each confirmed successful spawn_agent dispatch.
Every 6 confirmed new developer dispatches:
update manifest memory
call gobby-sessions.compact_self
resume from the summary and manifest memory
Reset `dispatches_since_compact` to `0` after compaction.

Idle behavior:
If agents are running and no useful coordinator work remains, check context health.
Use compact_self for context pressure or after the six-dispatch threshold.
Use bounded wait_for_agent only as the last idle action with `timeout_seconds = 300`.

Stop condition:
Stop when all eligible leaves across queued and active sections are closed or blocked.
Produce a final handoff manifest for external review/merge agents listing:
every section epic
branch
base branch
live worktree_id/path if present
closed leaves
blocked leaves
validation notes
review handoff status
