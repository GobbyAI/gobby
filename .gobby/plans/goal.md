# Finish #15764 with native Codex subagents

Complete #15764 and its fixes epic #15766. Act only as coordinator. Delegate every implementation and independent review to native Codex collaboration subagents. Use isolated campaign worktrees based on local `0.5.0`, merge approved sections sequentially, and own every newly discovered defect through closure under coordination epic #17824.

## Non-negotiable coordinator contract

- Use native Codex `spawn_agent`, `followup_task`, `send_message`, `list_agents`, and `wait_agent` only for subagent coordination.
- Never use `gobby build`, `gobby-agents`, Gobby personas, or Fable.
- Ignore #15764's historical Fable guard for this fixes phase. Never inspect Claude transcripts or enforce a model check.
- Treat live `gobby-tasks` and `gobby-worktrees` MCP state, git state, and native Codex agent state as authoritative.
- Use gobby-memory only for a resumable campaign manifest.
- #17824, `Coordination fixes: finish repo-wide review epic`, is the direct child coordination epic beneath #15764. Create every newly discovered bug or integration defect beneath #17824 before fixing it.
- The coordinator owns discovered issues through closure. Source changes remain delegated.
- Preserve unrelated worktrees and unrelated user changes.

## Queue and deterministic selection

- Rebuild the queue from open direct child epics of #15766 whose titles match `Review fixes: <slug>`.
- Order section epics by priority, then sequence number.
- Within a section, order ready descendant leaves by priority, then sequence number.
- Skip closed, escalated, blocked, or claimed leaves after recording their live state.
- Continue until every section descendant and every associated #17824 defect is closed.

## Capacity and campaign worktrees

- Maintain at most six #15764 campaign worktrees and at most six native Codex subagents, further limited by available native runtime slots.
- Maintain exactly one campaign worktree per active section epic and at most one live native agent for that section. Reviewer and implementer share the same section slot.
- Existing unrelated worktrees are outside the campaign count and remain untouched.
- Create section worktrees through `gobby-worktrees.create_worktree` with:
  - `branch_name = "review-fixes/<section-seq>-<normalized-slug>"`
  - `base_branch = "0.5.0"`
  - `task_id = <section ref>`
  - `use_local = true`
- Local `0.5.0` is the source of truth. Never pull, reset, or substitute `origin/0.5.0`.
- Reuse the section worktree for every leaf, discovered-defect repair, and review round.
- Remove a merged worktree before opening its replacement.

## Developer dispatch

Assign each developer exactly one ready leaf and one explicit worktree path. Its prompt must require:

- Work only in the assigned worktree and read that worktree's `AGENTS.md`.
- Use `gcode` and progressive MCP discovery.
- Claim the assigned leaf before editing.
- Run focused validation. Prefix every pytest invocation with `GOBBY_TEST_PROTECT=1`.
- Never run the full pytest suite.
- Fix every error, warning, failure, or bug encountered.
- Before fixing a discovered defect, create a specific task beneath #17824 with validation criteria and implementation domain, then claim it.
- Fix discovered defects in the same section worktree using task-linked commits.
- Commit as `[gobby-#NNNNN] <type>: <summary>`.
- Close completed tasks through `close_task(..., commit_sha=..., changes_summary=...)`.
- Leave the worktree clean and report task refs, commits, validation commands, and blockers.
- Never spawn another agent.

If a worker terminates unexpectedly, inspect live task, git, and worktree state before redispatch. Recover in the same verified worktree. Never run duplicate agents for one section.

## Finalization lane: sync, review, repair, approve

- Only one section may sync, review, and merge at a time. Other sections may continue implementation.
- Before review, merge the latest local `0.5.0` into the section worktree and run focused validation.
- Record the exact local `0.5.0` SHA as the review base.
- Launch a fresh native Codex reviewer that did not implement the final changes. Require inspection of:
  - the full section task tree and validation criteria;
  - linked commits and validation evidence;
  - `git diff <review-base-sha>...HEAD`;
  - spec compliance, correctness, security, architecture fit, code quality, testing, and proportionality.
- `approve` requires zero blocking findings and a clean worktree.
- A reviewer owns every discovered defect: create and claim a task beneath #17824, repair it in the same worktree, validate, commit, close the task, and return `changes_made`. A round containing changes cannot approve.
- After reviewer changes, launch a different fresh reviewer. Repeat until an unchanged round returns `approve`.
- Preferences may be non-blocking notes. Test failures, lint warnings, type errors, correctness gaps, and missing required coverage are blocking.

## Merge and closure

- Immediately before merge, verify local `0.5.0` still equals the recorded review-base SHA. Any change requires resync, focused validation, and a fresh review.
- Merge approved work with `gobby-worktrees.merge_worktree`, `target_branch="0.5.0"`, `push=false`.
- Run the section's focused validation against merged local `0.5.0` and verify all reviewed commits are ancestors of `0.5.0`.
- Close the section epic with merge SHA plus review and validation summary.
- Delete the merged campaign worktree and record, then open the next queued section worktree.
- After all #15766 section epics are merged and closed:
  - finish and close every #17824 child task;
  - close #17824;
  - close #15766;
  - verify every other direct child of #15764 is closed;
  - close #15764.
- Never push or create a PR without a separate user request.

## Resumable manifest

Maintain a gobby-memory manifest with one record per section containing:

- section ref/title/priority/sequence;
- branch, worktree ID, and worktree path;
- active leaf and active native Codex agent;
- closed leaves and #17824 coordination defects;
- validation evidence;
- review base, reviewer, round, and verdict;
- merge SHA and current status.

Rebuild the manifest from live tasks, git, worktrees, and native agent state after startup or compaction. Live state always wins. Persist before compaction and never compact while a child result is unrecorded.

Use bounded `wait_agent` calls only when agents are running and no coordinator work remains.

## Completion condition

Continue until all conditions hold:

- Every #15766 section epic is independently reviewed, merged into local `0.5.0`, and closed.
- Every #17824 coordination defect is fixed and closed.
- No #15764 campaign worktree remains.
- #15766, #17824, and #15764 are closed.
- Local `0.5.0` is clean.

Stop early only for a user-only decision or unrecoverable external blocker. Preserve the full manifest and continue all unaffected work first.

## Bootstrap snapshot (2026-07-11; verify live)

- #15766 has 69 direct `Review fixes:` epics: 10 closed and 59 open.
- No `review-fixes/*` local branch or campaign worktree existed at bootstrap.
- Local `0.5.0` existed at `a91cb03d28e9cf3a563c36d09f4e6072d58a0320` and intentionally diverged from `origin/0.5.0`.
- Main worktree contained three unrelated user-owned plan-artifact changes; preserve them and never include them in campaign commits.
- Effective native subagent capacity is `min(6, available child slots)`.
