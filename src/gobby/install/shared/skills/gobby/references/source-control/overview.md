# Source control

Load when working with commits, isolated repositories, synchronization or delivery.
This overview is a menu; listing an operation does not authorize or execute it.
Load the operation topic before acting, then lease the current tool schema.

| Topic | Load for | Invocation |
| --- | --- | --- |
| Commits | Stage, review and commit owned changes | `$gobby source-control references commits` |
| Worktrees | Discover, create, claim or reuse a worktree | `$gobby source-control references worktrees` |
| Clones | Work with an independent repository copy | `$gobby source-control references clones` |
| Synchronization | Update an isolated branch or publish it | `$gobby source-control references synchronization` |
| Merge campaigns | Survey, resolve and land local work | `$gobby source-control references merge-campaigns` |
| PR delivery | Apply branch policy and record delivery evidence | `$gobby source-control references pr-delivery` |
| Cleanup | Release, retire or delete managed isolation | `$gobby source-control references cleanup` |

Discover `gobby-worktrees`, `gobby-clones` and `gobby-merge` tools. Task ownership
and close gates belong to [tasks](../tasks/overview.md); worker spawning and
completion belong to [agents](../agents/overview.md). A worktree/clone claim is
separate from a task claim. Inspect existing task artifacts before creating more
isolation, and preserve another session's uncommitted files.

Managed workspaces must be landed and deleted, or explicitly handed off, before
their owning task is finished. A resolution SHA is not necessarily a final target
landing SHA. A local merge is not remote publication. Follow the applicable topic
and inspect the operation result before advancing dependent work.

Operator CLI/HTTP surfaces are documented in the
[worktree guide](../../../../../../../../docs/guides/worktrees.md).
The source-control HTTP API also exposes repository status, branches, commits,
diffs, PRs/checks, issues and CI runs. Select the intended project before using
that UI/operator surface. Its branch-checkout mutation changes the selected
checkout; preserve ownership and dirty work before any branch switch.
