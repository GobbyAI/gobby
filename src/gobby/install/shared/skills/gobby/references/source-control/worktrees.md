# Worktrees

Load when discovering, creating, claiming, reusing or changing worktree metadata.

Discover `gobby-worktrees:list_worktrees`, `get_worktree`, `get_worktree_by_task`
and `get_worktree_stats`. Resolve ambiguous references to exact IDs and inspect
task artifacts and current ownership before creating another workspace.

1. Choose the intended local base branch and task. Prefer worktrees for ordinary
   branch isolation; they share Git objects/remotes but have their own index/HEAD.
2. Use `create_worktree` with the intended branch and project context. Optional
   `worktree_path` overrides the generated path under
   `~/.gobby/worktrees/<project>/<safe-branch>`. Remote-style base names are refused.
3. `use_local` controls base selection; its default detects unpushed local work.
   `provider` optionally installs hooks. Check the returned creation result,
   including existing-worktree diagnostics, before assuming a new row was created.
4. Claim with `claim_worktree`; use `link_task_to_worktree` for an existing row.
   Release ownership with `release_worktree` when handing off. These metadata
   transitions neither merge nor delete the workspace.
5. Reuse a prepared worktree with `spawn_agent(worktree_id=...)`; follow the
   [agent isolation](../agents/isolation.md) reference for spawn precedence and
   recovery. Preserve a reused checkout when a rebase reports conflicts.

Persisted states include active, stale, merged and abandoned; ownership is a
separate session field. `abandon_worktree` and `reactivate_worktree` change
metadata, not branch contents. Inspect before reactivation or reuse. Detached
worktrees cannot be synced, pushed or marked merged through branch operations.

Project and machine context matter: another machine's path is not usable locally.
On a context/ownership error, select the correct project and machine instead of
adopting a foreign path. Creation writes project context into the isolated checkout.

Continue with [synchronization](synchronization.md) and [cleanup](cleanup.md).
See [worktrees](../../../../../../../../docs/guides/worktrees.md#creation).
