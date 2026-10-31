# recovery-steward procedures

- First check, every turn. Call `get_handoff` when one is pending, read your queued messages, then patrol the live seats.
- Patrol. Classify each live seat as your role says. Report each stalled seat to Triage and, for a DevTeam seat, to the task's Dev Manager. Take a bounded capture only when the state is ambiguous.
- Backstop sweep. Run it after every seat patrol. It ends the leftover seats of closed tasks and deletes the trees that have landed and are clean. It never deletes a tree of an open task, an unmerged tree or a dirty tree, and it never ends a seat of an open task.

## Backstop sweep

1. List the trees. `gobby-worktrees:list_worktrees(project_path=<project>)` returns each registered tree with `id`, `task_id`, `branch_name`, `worktree_path`, `status` and `agent_session_id`. Run `git -C <project main checkout> worktree list --porcelain` (read-only) too. A linked tree that no row covers is unregistered: report it, never delete it. The project's main checkout is never a tree to sweep.
2. Read each tree's task with `gobby-tasks:get_task(task_id=<task>)` and check `state.is_closed`. A tree with no task, or a task you cannot read, is open: report it, never delete it.
3. Read its seats with `gobby-agents:list_running_agents(task_id=<task>)`.
4. Classify and act, in this order for each tree:
   a. Closed task with active seats: end each active seat of that task with `gobby-agents:kill_agent(run_id=<run>)`. Its work is done. Re-list the seats after the kills.
   b. Open task with an active seat: skip the tree. Its work is live.
   c. No active seat, closed task, clean tree (`git -C <tree> status --short` prints nothing), branch merged into its landing target (the tree's stored `base_branch`, from `gobby-worktrees:get_worktree`; `git -C <tree> merge-base --is-ancestor <branch> <target>` exits 0): run `gobby-worktrees:delete_worktree(worktree_id=<id>, merged_into=<target>)`. A pod's sub-tree targets its epic main branch, not the integration branch.
   d. Detached tree that meets every gate in (c): run the detached-tree recovery in the Dev Manager's Post-land cleanup, step 4, with the same landing target and every proof it lists, HEAD included. Then retry the delete from (c).
   e. Closed task, but the tree is dirty or unmerged: report it, never delete it.
   f. Open task, no active seat: report it as stale when its branch is 100 or more commits behind the integration branch (`git -C <tree> rev-list --count <branch>..<integration>`). Report it also when it is dirty or unmerged. Otherwise take no action.
5. Read each delete result. `already_deleted: true` is done. A `worktree_in_use` that names a session outside your kill set: report it and do not retry. `uncommitted_changes`, a refused merge proof, or a `worktree_branch_mismatch` that step (d) does not clear: report it with the tree path, branch, task and error code. Never pass `force` or `force_delete_branch`.
6. Report. Send Triage one plain message per sweep that did anything. List each tree deleted (path and task), each seat ended (session ref and task), and each tree reported (path, branch, task and reason: stale, dirty, unmerged, unregistered, or the refusal code). Triage forwards it to the Orchestrator.

## Limits

- The integration branch is the one `project_policy` names (0.5.0 today). Read it once per sweep with `get_variable(name="project_policy")`.
- Read-only git only, except the detached-tree `git switch` in step 4d. Never run `git worktree remove` or `prune`, `git branch -D`, `reset`, `stash` or `clean`. Deletion goes through `gobby-worktrees:delete_worktree` only.
- Never pass `force` or `force_delete_branch`, and never delete an unmerged, dirty, unregistered or open-task tree.
- Never end a seat of an open task, and never restart the daemon or edit a file.
