Coordinate parallel implementation of repo-wide review section epics under #15766 using gobby-agents developer workers.

Role: coordinator only. No review work. No merge work.

Prohibited actions:
Source file edits.
Leaf task implementation by the coordinator.
QA, reviewer, or merge agent spawning.
Review, merge, push, mark merged, delete, or abandon worktrees.
Closing #15766.
Using gobby-agents.wait_for_agent.

Use MCP only:
gobby-tasks for task lifecycle, leaf inspection, and stage inspection.
gobby-worktrees for section worktrees.
gobby-agents for developer workers and completion reconciliation.
gobby-memory for the live coordinator manifest.
gobby-sessions only for compact_self.

Base branch:
All section worktrees must be created from local `0.5.0`.
Use `base_branch = "0.5.0"` when creating worktrees.
Before creating worktrees, verify local `0.5.0` exists.
If local `0.5.0` is missing or unavailable, stop and report the blocker.

Queue:
Build the queue from direct child epics of #15766.
Each direct child epic is one section.
Create or reuse one worktree per active section.
Link each section epic to its worktree.
Branch names: `review-fixes/<section-slug>`.

Concurrency:
Start with at most 6 active section worktrees.
Raise to 8 only after the first wave completes cleanly.
Run at most one active developer worker per section worktree.

Coordinator manifest:
Maintain live manifest memory `ebb7269b-7083-55ef-ad92-47a0b653c583` with:
section epic ref
section title
worktree_id
worktree path
branch name
base branch
active leaf ref
active agent run_id
closed leaf refs
blocked leaf refs with reasons
validation notes
status: queued, active, ready_for_external_review, blocked

Current handoff state:
Manifest memory is stale and must be updated before dispatch resumes.
Verified closed valid:
adapters #16134: run-a5a4b4cb9ebc, commit 129005b29, wt-73b652 clean
cli-build-ops #16115: run-a5f2ecf5578f, commit 6ca2f47db, wt-af8547 clean
build #16040: run-4888cba3b39a, commit de1254693, wt-71e4b9 clean
agents #15875: run-2d890f6d39df, commit ad4ec0a27, wt-388efd clean
cli-core #16164: run-e5388a2b3ea9, commits 731d7f9b1 and cfa256509, closed commit cfa256509, wt-2c86eb clean
Unresolved:
ai #16110: run-db3f744403a5 failed with provider stall.
#16110 remains open, unclaimed, validation pending.
wt-338f22 has uncommitted changes; inspect and continue in that same worktree.
Latest reconciliation showed no running workers from the tracked refill batch.

Worker dispatch:
Dispatch one leaf task at a time per active section.
Select the next open, unclaimed, non-escalated leaf under the section in priority order: 1, then 2, then 4.
Choose developer agent from leaf `implementation_domain`:
backend -> backend-developer
frontend -> frontend-developer
fullstack -> fullstack-developer
Spawn with gobby-agents.spawn_agent:
agent = selected developer
task_id = leaf task ref
worktree_id = section worktree id
isolation = inherit
notify_parent_on_completion = true
If a spawn call is interrupted, reconcile with list_agent_runs before dispatching again.

No review gates:
Before dispatching a leaf, inspect get_task_stages.
Dispatch only leaves whose stage manifest allows developer self-close.
If a leaf has a development review gate or would require submit_for_review, record it as blocked_by_review_gate and continue with another
eligible leaf.

Developer worker requirements:
Claim the assigned leaf before editing.
Work only on the assigned leaf.
Commit with `[gobby-#NNNNN] ...`.
Run focused validation for touched code.
Close the leaf with close_task and commit_sha.
Call end_agent_run before exiting.
Escalate blockers through gobby-tasks.

Coordinator loop:
On daemon activity, call deliver_pending_messages(target_session_id=<parent session>).
Reconcile with list_agent_runs(parent_session_id=<parent session>).
Use get_agent_result only for terminal runs.
When a worker finishes, refresh that task and section worktree.
If the task closed valid and the worktree is clean, update the manifest and dispatch the next eligible leaf in the same section.
If a worker fails with provider stall, inspect task and worktree state. If the worktree is dirty, retry by continuing in that same worktree.
If all leaves under a section are closed or blocked, record section status ready_for_external_review.
Close a section epic only when every child leaf is closed or explicitly blocked and the worktree status is recorded as ready_for_external_review.
Keep #15766 open.

Compaction:
Maintain `dispatches_since_compact`.
Increment after each confirmed successful spawn_agent dispatch.
Every 6 confirmed dispatched subagents:
update manifest memory
call gobby-sessions.compact_self
resume from summary and manifest memory
Also compact after a full six-slot launch/refill batch.

Stop condition:
Stop when all eligible leaves across queued sections are closed or blocked.
Produce a final handoff manifest for Claude Fable review/merge agents listing:
every section worktree
branch
base branch
closed leaves
blocked leaves
validation notes
review handoff status
