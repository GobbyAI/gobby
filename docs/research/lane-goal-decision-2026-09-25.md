# Lane `/goal`: retire, repair, or replace with a lane-epic rule (#22924)

Researcher gobby#14550, 2026-09-25. Evidence only; no production or role edits.
Claims are marked VERIFIED (checked against the DB, code or messages) or INFERRED.

## Recommendation

**Retire `/goal` from developer lanes. Keep lanes on the controls Gobby already
has. Leave `require-epic-tree-close` disabled for now.**

`/goal` adds nothing that the claimed-task stop gate does not already do durably.
It is also the direct cause of all three Lane 1 stalls today. The lane-epic rule is
workable, but it changes who picks a lane's next task, which conflicts with the
PD-ordered queue. Treat it as a separate decision only if routing between leaves
turns out to be slow.

## The three Lane 1 incidents (1-2 VERIFIED in inter_session_messages; 3 reported by the PD)

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

So `/goal` breaks on all three events a lane routinely meets: restart,
compaction, and a coordination HOLD.

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
compactions and coordination HOLDs.

## What already keeps a lane on task (VERIFIED)

| Control | Where | Survives compaction or restart |
| --- | --- | --- |
| Claimed task | `claim_task`; session variable `claimed_tasks` | Yes. Session state and the DB claim both persist. |
| Stop-hook hold | `require-task-close` (enabled): the turn cannot end with a claimed task open unless there is a legal exit (close, `wait_for_agent`, `wait_for_coordination`, `add_dependency`, `escalate_task`) | Yes. It is evaluated on every Stop. |
| Handoff continuity | `set_handoff` / `get_handoff`; the Stop fallback since #22803 (memory 2e392c09) | Yes. Delivery is recorded in `session_handoff_deliveries`. |
| Idle detection | Lane Manager `wait_for_coordination(statuses=[paused, awaiting_input, awaiting_approval, awaiting_handoff, interrupted])` (lane-manager.md). A DB trigger resolves it on `sessions.status` changes. | Yes. The waits are durable rows. |
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

Implementation, owned by the PD with a small change:
- Stop sending `/goal` at lane kickoff. No role file references it today, so it
  comes from the kickoff instructions or a manual start.
- Clear any active goals on current lanes.
- Add one line to `_common.md` or the lane role template: lanes do not use `/goal`.
