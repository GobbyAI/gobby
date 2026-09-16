# Synchronization

Load before updating a workspace from another branch or publishing branch commits.

Discover `sync_worktree`, `push_branch` on `gobby-worktrees` and `sync_clone` on
`gobby-clones`. Read the exact row and intended source/target before mutation.

1. Inspect ownership, uncommitted files and in-progress merge/rebase state.
   Preserve foreign work; recover existing operations before starting another.
2. `sync_worktree` uses its stored base unless `source_branch` is supplied.
   Strategy defaults to merge; rebase is explicit. Operator `sync --source`
   forwards the supported source argument and uses the default merge strategy.
3. Inspect sync success and conflicts. A failed rebase does not authorize
   abandoning the existing worktree or replacing its branch with a fresh one.
4. Clone synchronization uses its configured remote and pull/push/both direction.
   Check the Git remote, ancestry and authorized publication scope first.
5. For worktree publication use `push_branch`. `branch` is the local source;
   `target_branch` is the destination remote branch and defaults to the source.
   Publishing a PR source branch normally uses that branch's own name.

`merge_worktree` and `merge_clone` are local landing operations; they do not
replace publication or PR review. Passing push/prefer-remote options to force
local landing into remote delivery is unsupported. Open a GitHub PR through the
GitHub MCP server when that publication is authorized.

The worktree push implementation invokes Git with `--no-verify`; complete the
repository's required checks explicitly before publication. Do not infer a green
pre-push hook from a successful tool result. Use `force_with_lease` only for an
authorized rewritten source branch after checking its remote state.

On a timeout or connection loss, inspect the remote/target and operation state
before retrying. A successful transport is not proof of a successful Git result.
See [sync and merge](../../../../../../../../docs/guides/worktrees.md#sync-and-merge).
