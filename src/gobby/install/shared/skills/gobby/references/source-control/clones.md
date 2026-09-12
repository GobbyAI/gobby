# Clones

Load when independent repository state is required beyond worktree isolation.

Discover `gobby-clones:list_clones`, `get_clone`, `get_clone_by_task` and
`get_clone_stats`. Inspect the exact row, task link and machine ownership.

1. Prefer a worktree unless a separate object store and remote state are needed.
2. `create_clone` requires a branch and path. Remote cloning uses the requested
   branch with shallow depth by default; that branch must exist at the source.
   `use_local=true` instead makes a full local clone from the base branch and
   creates the requested branch, preserving unpushed local commits.
   The path must resolve below `~/.gobby/clones`, for example
   `~/.gobby/clones/feature-auth`; creation and deletion reject paths outside it.
3. Supply the intended task and source. Task references are resolved before Git
   work; missing project context is rejected before file creation. Inspect
   failures and any cleanup diagnostics before retrying a destination path.
4. Use `claim_clone`, `release_clone` and `link_task_to_clone` for ownership and
   task metadata. Reuse the clone through `spawn_agent(clone_id=...)` with the
   [agent isolation](../agents/isolation.md) guidance.
5. `sync_clone` supports pull, push and both. It temporarily marks syncing and
   restores active state; inspect `success`, because active does not prove sync
   succeeded. Authorize remote writes before selecting push/both.
6. Use `merge_clone` for final local landing, then [cleanup](cleanup.md). The
   landing result and target SHA are the evidence; do not substitute a source SHA.

Landing fetches directly from the local clone into a temporary branch in the
target repository; it does not push to origin. Unrelated staged target changes
use a fast-forward landing path, while overlapping dirt is refused. Inspect
cleanup warnings even after success: a failed stash restore retains its exact
object ID for recovery. `landing_state=unknown` means target evidence could not
be read; inspect Git before retrying or authorizing deletion.

Clone creation, sync, landing and deletion can outlive a transport timeout.
Inspect the exact row, checkout and result before retrying; avoid duplicate work.
Detached clones have no branch to synchronize or land. A shallow history may
need an authorized history fetch before a merge can find its ancestry.

Operator `gobby clones` commands include list, create, spawn, sync, merge and
delete. Their JSON mutation output preserves failure exit status. CLI creation
does not expose every MCP option; consult the live schema for local/full cloning.

See [clones](../../../../../../../../docs/guides/worktrees.md#clones).
