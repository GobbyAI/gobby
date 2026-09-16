# Droid Compatibility Probe — Task #22402

Live probe of Gobby's daemon behavior with Droid as the agent provider. Run by an
observer session (Gobby session lineage gobby#13554 → gobby#13569 after compaction)
in `/Users/josh/Projects/gobby`, project `d45545c5-ded5-4335-b115-0245752edacf`.
Task #22402 is owned by session gobby#13482; the probe never claims, edits, or
closes it. Documentation-only: the observer made no code changes. All child-agent
evidence was produced by spawned Droid children, not the observer.

Probe environment: provider `droid`, model `glm-5.3-flash`, terminal backend
`tmux`. This file is the working-notes file referenced by the probe 7 handoff,
written under docs task #22440.

## Results Summary (Probes 0–6)

| Probe | What was tested | Result | Expect met |
| --- | --- | --- | --- |
| P0 | Project announcement via `send_message` | Delivered to 7 sessions (broadcast 53a8cee1) | Yes |
| P1 | Spawn without task (self-claim workflow) | Spawn OK; child's `create_task` blocked by rule gate; run still reports success | Partial — finding |
| P2a | `stop_agent` on a running child | Cancelled cleanly, `user_cancelled` | Yes |
| P2b | `kill_agent` on a running child | Observationally equivalent to stop | Yes |
| P3 | Blocker round trip via a real task | Full round trip worked after resolving two new gates; same worktree reused on respawn | Yes |
| P4a | Native Skill tool menu for `gobby` | Full capability menu returned | Yes — fix confirmed |
| P4b | Worktree hygiene (`git status` clean, no `.factory/` entry) | Clean in both surviving worktrees | Yes — fix confirmed |
| P5 | Worktree cleanup (`delete_worktree force=true`) | One deleted; one refused with `branch_not_merged_into_base` | Partial — finding |
| P6 | Handoff + `/compress` compaction | Compaction verified; staged handoff not retrievable via `get_handoff` afterward | Partial — finding |

## Probe Details

### P0 — Announce

`send_message` with `target=project` delivered to 7 sessions. Broadcast id
`53a8cee1`.

### P1 — Self-claim spawn

`spawn_agent` (droid, glm-5.3-flash, tmux, `isolation=worktree`, no task_id):

- run `38962779-523f-4ca3-af9c-70d5da4954be`
- child session gobby#13557
- worktree `e46541a8-25f1-4b83-9319-a3309deb6928`

The child's `create_task` was rejected twice by rule gate
`block-spawned-agent-create-task`: no task, no doc, no commit. The run still
reported `success` and `terminal_reason` was null. **Finding: run success masks
gate-blocked failure** — a parent observing only run status cannot tell the child
was forbidden to do its assigned workflow.

### P2a — stop_agent

Spawn with `isolation=none`: run `35af8e2e-f8ab-4720-96d4-45d33e1253e3`, child
session `7cba3e5e-…`. After polling `get_running_agent` until
`tool_calls_count=1`, `stop_agent` produced status `cancelled`, terminal_reason
`user_cancelled`, `completed_at` 18:55:58Z. Child pid 41488 dead, pane gone,
session expired.

During investigation a live droid pane `%14` was seen; it belongs to a foreign
run (`GOBBY_AGENT_RUN_ID=39f47f1d-…`, different probe prompt) and is unrelated.

### P2b — kill_agent

New spawn: run `d7050544-cda5-4573-a2ff-1eb93ae65168`, child session
`f7d23d73-…`. After `tool_calls_count=1`, `kill_agent` closed the terminal via
`runtime_terminate`; status `cancelled`, terminal_reason `user_cancelled`,
`completed_at` 18:57:26Z, `session_expired` true, child pid 48004 gone.
**Finding: `stop_agent` and `kill_agent` are observationally equivalent.**

### P3 — Blocker round trip

1. `create_task` was first blocked by rule `require-tasks-skill-for-mutations`;
   after loading `references/tasks/overview.md` and `references/tasks/creation.md`
   the retry succeeded. Task #22434 (`18e0f543-af5c-4314-8fa5-79a479a3b555`)
   created unclaimed, with docs and labels `droid-probe` / `provider-compat`.
2. Spawn with `task_id` and worktree isolation: run
   `ca368090-5bb5-47ed-9569-983c59a3f737`, child gobby#13562, worktree
   `6803ea27-4cd3-40c9-834f-8d936146b156`, branch
   `task-22434-droid-probe-blocker-round-trip`. The blocker question ("Which
   word should I write?") was delivered and received. The child escalated the
   task while waiting; run reported `success`.
3. Respawn attempt refused: `Task #22434 is not actionable; refusing to spawn
   agent`. `reopen_task` also refused: `Task #22434 is controlled by active
   build automation. Run gobby build stop #22434 before reopening it.` The
   task-scoped `gobby build stop '#22434'` (quote the `#` argument — zsh ate the
   unquoted form as a comment) stopped the automation (agents: 0), and
   `reopen_task` then succeeded.
4. Respawn with "The word is marzipan. Finish the task.": run
   `86fccd6d-81bf-410c-aadd-2c2647ad57cf`, child gobby#7f5ef57b, **same worktree
   `6803ea27` reused**. Run completed `success`; task #22434 closed completed
   with `closed_commit_sha 31f53f3166`, validation valid, and
   `docs/droid-probe-blocker.md` containing exactly `marzipan`.

Child-reported friction: a `require-code-review-skill` hook blocked its first
commit; the close criterion command had to run verbatim; `wait_for_agent`
refuses to run in headless Droid sessions.

### P4a/P4b — Regression checks

- P4a: the native Skill tool with name `gobby` returned the full capability menu
  via the router's error-shaped response. Confirms gobby#13482's fix.
- P4b: `git status --short` in both surviving probe worktrees is clean with no
  `.factory/` entry. Confirms the second fix.

### P5 — Worktree cleanup

`delete_worktree` with `force=true`:

- `e46541a8-25f1-4b83-9319-a3309deb6928`: deleted OK.
- `6803ea27-4cd3-40c9-834f-8d936146b156`: refused with exact error
  `branch_not_merged_into_base` despite `force=true`. Left in place to preserve
  evidence commit `31f53f3166`.

**Finding: `force=true` does not override `branch_not_merged_into_base`.**
**Open item:** the owner must merge branch
`task-22434-droid-probe-blocker-round-trip` or explicitly decide on
`force_delete_branch`. Do not destroy commit `31f53f3166`.

### P6 — Handoff + compact

First `set_handoff` was blocked by an aggregated 3-gate rule. Cleared by:

1. fully reading `references/sessions/handoffs.md`,
2. `review_task_memories` for #22434 (5 candidates, all accurate, no memory
   write; gate released),
3. submitting 3 feedback observations (ids `e899a8a9`, `da0b04db`, `deeef6c9`).

Retry `set_handoff(clear_session=false)` succeeded:
`handoff_staged: true, delivery_pending: true`, attempt_id
`5396c81e373c4cfb88488973cd8dc8fa`, command `/compress` via tmux. The daemon
interrupted the turn ("Request cancelled by user" / "Request interrupted by
user") — the expected dispatch signal.

Verification of the compacted continuation (session re-registered
gobby#13554 → gobby#13569):

- Compaction happened: the continuation resumed from a compaction summary. ✔
- Handoff visibility: no-argument `get_handoff` returned
  `{found: false, session_id: null, handoff: ""}` — the staged compact handoff
  is not consumable by the compacted session itself; the results survived only
  through the compaction summary. **Finding / deviation.**
- The post-compaction turn still reported "Context is 3131k tokens" and rule
  `require-handoff-at-context-limit` blocked `ToolSearch` on the first call of
  the fresh session; the whitelisted handoff-prerequisite path worked.

Feedback filed this epoch: `18a0e0d6` (surprise: `get_handoff` empty after a
`clear_session=false` compact) and `460ce845` (noise: stale context counter at
resume).

## Findings Recap

1. `block-spawned-agent-create-task` blocks a spawned child's task creation
   while the run still reports `success` (P1).
2. An escalated task blocks both `spawn_agent` and `reopen_task` until a
   task-scoped `gobby build stop '#N'` plus reopen (P3).
3. `delete_worktree force=true` does not override `branch_not_merged_into_base`
   (P5).
4. `stop_agent` and `kill_agent` are observationally equivalent (P2).
5. P4a/P4b confirm gobby#13482's fixes work (P4).
6. A staged compact handoff (`clear_session=false`) is not retrievable via
   `get_handoff` after `/compress`; content reaches the continuation only via
   the compaction summary (P6).

## Open Items

- Worktree `6803ea27-4cd3-40c9-834f-8d936146b156` still exists; deletion is
  refused even with `force=true`. Merge
  `task-22434-droid-probe-blocker-round-trip` or decide on
  `force_delete_branch`. Evidence commit `31f53f3166` must survive.
- Probe 7 (`set_handoff` with `clear_session=true`) is staging as this file is
  written; expect `/clear`, a new session, and `get_handoff` returning content.
- Probe 8 (successor session): send the full results table with exact error
  text to gobby#13482 (`target=session`), announce the probe finished to the
  project (`target=project`; daemon restart acceptable), then print the table.
  Constraints: never claim, edit, or close task #22402; no code changes.
