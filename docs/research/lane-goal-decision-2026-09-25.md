# Lane `/goal`: retire, repair, or replace with a lane-epic rule (#22924)

Researcher gobby#14550, 2026-09-25. Evidence only; no production or role edits.
Claims are marked VERIFIED (checked against the DB, code or messages) or INFERRED.

## Decision (Josh, 2026-09-25)

**Retire `/goal` from developer lanes. Lane epics stay off.** Josh confirmed it
with the button and in text, through the Assistant #14069; the PD #14543 relayed
it.

- Implementation: #22927 "Retire lane /goal and make Lane Manager recover stalled
  lanes". Owner: Lane 3 #14531, in an isolated worktree after #22912. The PD
  reviews and lands it.
- One-time clearing of existing goals: the Assistant, using `/goal clear` at each
  affected lane's idle prompt; the Lane Manager #14556 lists which seats need it.
- `require-epic-tree-close` stays disabled.

## Recommendation

**Retire `/goal` from developer lanes. Keep lanes on the controls Gobby already
has. Leave `require-epic-tree-close` disabled for now.**

`/goal` adds nothing that the claimed-task stop gate does not already do durably.
It is also the direct cause of all four Lane 1 stalls today. The lane-epic rule is
workable, but it changes who picks a lane's next task, which conflicts with the
PD-ordered queue. Treat it as a separate decision only if routing between leaves
turns out to be slow.

## The four Lane 1 incidents (1-2 VERIFIED in inter_session_messages; 3-4 reported by the PD)

Lane 1 is gobby#14544 (Codex) on epic #22773 (gclient chrome).

1. **About 14:25 CT, after a daemon restart.** Codex hit "control-plane unavailable"
   during the restart and its native goal paused: the pane showed
   `Goal paused (/goal resume)`. Gobby still recorded the session as `active`, so
   the durable DAEMON BACK and the `wake=true` GO (message 081419dc) were both
   declined as `session_active`. It recovered at 14:33 only after the Assistant
   sent `/goal resume` into the pane with one send_keys, on Josh's prompt.
2. **About 15:00 CT, after a compaction.** The session sat in `awaiting_handoff`
   with the goal paused. The handoff went unconsumed until `/goal resume` was sent
   by hand.
3. **About 15:30 CT, under a validation HOLD.** The pane showed
   `Goal stalled (/goal resume)`: the goal evaluator read the PD's HOLD as a
   blocked goal. The Assistant #14069 resumed it at the Lane Manager's request.
   (Reported by the PD #14543.)

4. **Right after the PD's validation GO.** Lane 1 stalled again. The Lane Manager
   and the Assistant resumed it, and the focused test then began. (Reported by
   the PD #14543.)

So `/goal` breaks on the events a lane routinely meets: restart, compaction, a
coordination HOLD, and the GO that ends one.

Why the HOLD and GO stalls happen (VERIFIED from strings in the codex-cli 0.156.1
binary): the model has an `update_goal` tool that "can only mark the existing goal
complete, blocked, or paused". When the model judges a HOLD or wait to be a
blocker, the pane shows `Goal stalled (/goal resume)`, and only a user
`/goal resume` restarts it.

### Separate issue: the Lane Manager's round missed incident 3 (not a `/goal` defect)

In incident 3, the Lane Manager's status wait matched, but the Lane Manager
treated the match as report-only while the HOLD was in force. Escalation waited
until Josh prompted. Per the PD, the Lane Manager now classifies every match
immediately. This was a miss in the Lane Manager's rounds; it is fixed in its
procedure and is independent of the `/goal` decision. Retiring `/goal` removes
the stall; the Lane Manager fix makes any future stall visible quickly.

Root cause (INFERRED from the above and the code): a paused Codex goal is
invisible to Gobby. Nothing maps the paused goal to a session status; the only
goal-related Gobby code is the unrelated `goal_file` variable in
`inject-autonomous-mode.yaml`. The session looks `active`, so:
- wakes are declined;
- the Lane Manager's status waits (`paused`, `awaiting_*`, `interrupted`) never
  fire, so the lane stalls silently.

The goal pauses on exactly the events Gobby is built to survive: restarts,
compactions, coordination HOLDs and the GO that ends them.

### Separate check: the PD's `get_handoff` returned found=false at 15:32 CT (not a defect)

The PD's continuation at 15:32 CT (20:32 UTC) called `get_handoff` and got
`found=false`. VERIFIED from `session_handoffs` and `session_handoff_deliveries`
for session 1abdbd31: every handoff the PD staged today was delivered.

| Staged (UTC) | Delivered (UTC) | Boundary |
| --- | --- | --- |
| 13:50:25 | 13:50:48 | compact |
| 14:27:57 | 14:28:15 | compact |
| 19:49:49 | 19:51:41 | compact |

No row was staged after 19:49 UTC, so nothing was pending at 20:32, and
`found=false` was the correct answer. INFERRED: the 15:32 continuation was a
native compaction with no `set_handoff` beforehand, which matches the PD's
summary still listing `set_handoff` as unfinished. This is separate from `/goal`,
and there is no handoff-delivery defect to route to Lane 3.

## What already keeps a lane on task (VERIFIED)

| Control | Where | Survives compaction or restart |
| --- | --- | --- |
| Claimed task | `claim_task`; session variable `claimed_tasks` | Yes. Session state and the DB claim both persist. |
| Stop-hook hold | `require-task-close` (enabled): the turn cannot end with a claimed task open unless there is a legal exit (close, `wait_for_agent`, `wait_for_coordination`, `add_dependency`, `escalate_task`) | Yes. It is evaluated on every Stop. |
| Handoff continuity | `set_handoff` / `get_handoff`; the Stop fallback since #22803 (memory 2e392c09) | Yes. Delivery is recorded in `session_handoff_deliveries`. |
| Idle detection | Lane Manager `wait_for_coordination(statuses=[paused, awaiting_input, awaiting_approval, awaiting_handoff, interrupted])` (lane-manager.md). A DB trigger resolves it on `sessions.status` changes. | Partly. The wait row is durable (VERIFIED: `coordination_waits`, resolved by the DB trigger). The wake notification is not reliable across a restart: OBSERVED by the Assistant, Lane 3's pre-restart `wait_for_coordination(reply=true)` did not notify after the restart, and the lane sat idle until a manual prompt at 15:52 CT. Tracked in #22860. |
| Next-task routing | PD orders the queue; Lane Manager wakes the seat with `wake=true` | Yes. Messages are durable. |

What `/goal` adds for a one-task developer lane: an auto-continue loop toward an
objective. The stop gate already refuses to let the turn end while the claimed
leaf is open, and Codex's evaluator is transcript-scoped, so it loses evidence
across compaction (memory 2e392c09). No extra value was found.

## Option 2: repair `/goal` (not recommended)

This would need all of:
- detecting Codex's native goal-paused state and mapping it to a Gobby status,
  so that wakes and status waits work;
- resuming the goal after compaction or restart. Today that means send_keys into
  a lane, which `_common.md` forbids ("never send keys into another session");
- repeating both for each provider's `/goal` (Claude's evaluator has the same
  transcript scope).

That is new cross-provider mechanism to keep a feature that duplicates the stop
gate. It fails restraint rung 1.

## Option 3: lane epic plus `require-epic-tree-close`

Josh's proposal: create a lane epic, load it, give the agent a leaf, let it work.

**Installed row (VERIFIED, `rule_definitions`).** The rule's real name is
`require-epic-tree-close`; there is no `require-epic-task-close`.
- id `a1575ce9-dcc8-47e9-a045-ae2707b1f4df`, source `installed`
- `enabled=false`, `enabled_pinned=true`: disabled by a user toggle that template
  sync preserves
- updated 2026-09-25 00:11:53 UTC (2026-09-24 19:11 CT)
- `agent_scope: [default]`, priority 51

**Why it was disabled (VERIFIED, Josh via the Assistant, 20:07 UTC).** "agents
were getting sent goals and it conflicted with found work and other rules."
Earlier messages show the pattern:
- 2026-09-01: a worker was told it must not close #21479, yet the rule demanded
  closing the epic #21473 tree.
- 2026-09-06: the rule kept #208 alive because #147 stayed claimed.
- 2026-09-10: it nudged closure of #22027 while an explicit "KEEP OPEN" was in
  force.

In each case coordinator-owned closure or explicit hold instructions collided with
a rule that demands the tree be closed.

**What it enforces today (VERIFIED, template plus
`condition_helpers.task_tree_complete`).**
- It blocks Stop while any claimed task, or a task in its subtree, is open, unless
  a durable stop wait exists.
- The parent epic itself may stay open. A tree counts as complete when the task
  is closed, or when it has subtasks and all of them are complete.
- For a session that claims only a leaf, it is equivalent to `require-task-close`.
  It bites only sessions that claim a parent or epic.

**Minimal safe lane-only contract, if Josh later wants it (INFERRED design).**
1. Scope: only a session that claims its lane epic is affected. Other sessions
   claim leaves, where the rule is a no-op. That makes it lane-only without a new
   scoping mechanism, provided no non-lane role is told to claim a parent. Add
   that to `_common.md`.
2. Found work on the lane's surface is filed as a child of the lane epic, so the
   rule keeps the lane until it is fixed; that matches "you found it, you fix it".
   Found work outside the lane goes to the PD and is not parented under the lane
   epic, so it cannot trap the lane. The found-work gate already exempts trees a
   session authored (memory 96abd67e).
3. PD review: after submitting a CANDIDATE, the lane registers
   `wait_for_coordination(reply=true)` on the PD. That is a durable wait and
   already a legal exit.
4. Close ownership: lanes close their own leaves after the PD lands them, as
   today. "KEEP OPEN" instructions must become a dependency or an escalation, not
   prose.

**Why not now.** Claiming the epic makes the lane pick its next child itself.
That moves queue order from the PD, through the Lane Manager, into the lane (or
into dependency edges the PD must maintain). It is a routing-authority change on
top of a stall that retiring `/goal` already fixes.

## Decision for Josh (one button)

**Retire `/goal` from lanes.** Lanes run on the claimed leaf, the stop gate, and
Lane Manager status waits. `require-epic-tree-close` stays disabled.

Implementation: #22927, owned by Lane 3 #14531 (see Decision above). The PD
reviews and lands it. Scope:
- Remove any `/goal` start or resume from the developer lane kickoff instructions
  and from shared role guidance, at their actual active source. No file under
  `.gobby/roles/` references `/goal` today (VERIFIED by grep), so it comes from
  the kickoff instructions or a manual start.
- Clear any active goals on current lanes with `/goal clear` (see below).

### Supported exit for a lane that already has a goal (VERIFIED, codex-cli 0.156.1)

- Command: `/goal clear`. The binary's usage line is
  `Usage: /goal [<objective>|clear|edit|pause|resume]`; on success Codex prints
  `Goal cleared`. There is no `/goal stop`.
- `/goal pause` is not an exit. It leaves the goal in `Goal paused (/goal resume)`,
  which is the same stalled state as incidents 1-2.
- Scope: `/goal clear` removes only Codex's own thread goal, which Codex keeps in
  its state DB. The Gobby claim (for example #22755), `claimed_tasks` and the
  staged or consumed handoffs live in Gobby's DB and are untouched. After the
  clear, `require-task-close` and the Lane Manager's status waits keep the lane
  on task.
- Procedure: type it in the lane's Codex composer while the lane is idle at the
  prompt. INFERRED: typed mid-turn, it may be queued as input. This is a
  keystroke into another session, so under `_common.md` it is done by Josh or by
  the Assistant on Josh's instruction. Confirm the pane shows `Goal cleared` and
  that `get_session` still lists the claimed task. Do not set a new `/goal`.
- Add one line to `_common.md` or the lane role template: lanes do not use `/goal`.
