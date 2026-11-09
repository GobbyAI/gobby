# Orchestrator: worktrees

Load before cleaning up, correcting or staffing an existing worktree, or approving a seat's dirty paths.

- Staffing an existing tree. A tree only falls behind its base through a work stoppage. Before a team is relaunched onto it, its lead has it merge-leveled (the task's developer merges the base in under its claim), every conflict resolved in favor of the newer code.
- Worktree cleanup. The completion owner confirms the task is closed, the registered tree is clean including untracked files, its branch is merged into its stored landing target, and no live task process uses it. Delete through the worktree tools and personally remove the merged branch. Correct a detached or mismatched tree only with explicit HEAD/branch proof; preserve any unresolved tree and report the blocker. Never pass `force` or `force_delete_branch`. Coordinate with live owners first.
- Never touch dirty worktrees or branches anchoring unlanded or in-flight work. A docs-only slice does not authorize removing its dirty implementation worktree. Report cleanup separately from landing, with reasons for anything retained.
- Approving a seat's dirty paths (operator ruling). A seat can lose ownership of its own uncommitted edit, for example one written during a daemon outage, and then its edits, commits and claim hand-offs are refused. Approve it yourself; never send the command to the operator. First confirm the path and its blob in that tree and that the seat wrote it, then back the file up in your workspace. Run `uv run --no-sync gobby sessions approve-dirty-paths <owning seat UUID> --checkout <tree root> --reason "<why the paths are released>" <paths...>` for the seat that wrote it, and tell that seat. Never approve a path for any other seat, and never use approval to clear an overlap with another live seat's work. Once the writer session has expired or ended, its successor seat can be approved for those paths. A live holder's uncommitted files block its hand-off until that holder's run ends.

_Version: 2.2.0_

_Last verified: 2026-11-08_
