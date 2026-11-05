# Orchestrator: worktrees

Load before cleaning up, correcting or staffing an existing worktree.

- Staffing an existing tree. A tree only falls behind its base through a work stoppage. Before a team is relaunched onto it, its lead has it merge-leveled (the task's developer merges the base in under its claim), every conflict resolved in favor of the newer code.
- Worktree cleanup. The completion owner confirms the task is closed, the registered tree is clean including untracked files, its branch is merged into its stored landing target, and no live task process uses it. Delete through the worktree tools and personally remove the merged branch. Correct a detached or mismatched tree only with explicit HEAD/branch proof; preserve any unresolved tree and report the blocker. Never pass `force` or `force_delete_branch`. Coordinate with live owners first.
- Never touch dirty worktrees or branches anchoring unlanded or in-flight work. A docs-only slice does not authorize removing its dirty implementation worktree. Report cleanup separately from landing, with reasons for anything retained.

_Version: 2.0.0_

_Last verified: 2026-11-05_
