---
name: goal
description: "Author and execute durable goal loops: intake -> draft .gobby/goals/<slug>.md -> confirm -> execute in solo or swarm mode against an anchor task. Use for /gobby goal, goal run/resume/status, autonomous epic burn-down, and compact-and-continue loops."
version: "1.1.1"
category: core
triggers: goal, goal loop, autonomous loop, burn down epic, run until done
metadata:
  gobby:
    audience: all
    depth: 0
    format_overrides:
      autonomous: full
---

# /gobby goal

A goal is an anchor task ref plus a procedure overlay. The anchor task tree in
gobby-tasks is the source of truth for state; the goal doc
(`.gobby/goals/<slug>.md`) is the durable procedure, budget, and resume
artifact. Executing a goal means working the anchor's tree in a loop — claim,
implement, close, compact, continue — until the tree is complete and the anchor
closes, in this session, a session you kick off, or a spawned agent.

This skill is operating instructions for the session executing or authoring the
goal. It layers on top of the auto-task rules: setting `auto_task_ref` gives
you objective injection, a turn-end stop-block until the tree is complete, and
a completion notice. The goal doc refines that machinery with your specific
procedure, budgets, and stop conditions.

## Invocation Forms

- `/gobby goal <objective>` — intake: draft a goal doc from an objective,
  a task ref, or a pasted micro-goal contract.
- `/gobby goal run <file>` — execute a confirmed goal doc.
- `/gobby goal resume [<file>]` — resume an active goal in this session.
- `/gobby goal status` — report the active goal's state from the doc and tree.

Codex sessions use the `$gobby` prefix (`$gobby goal run <file>`); CLIs with
the slash router use `/gobby goal`. Never install or invoke a bare `/goal` —
some CLIs ship a native `/goal` feature and this skill must not shadow it.

## Goal Doc Contract

Goal docs live in `.gobby/goals/<slug>.md`. Always write the file, even for
micro-goals — it is the resume artifact, the kickoff payload, and the input
for spawned executors. Do not register goal docs with the plans registry; they
are not plans and will not validate as one.

```markdown
---
goal: <short title>
status: draft | active | done | suspended | cancelled
anchor: "#18357"
mode: solo | swarm
created: 2026-07-17
sessions: ["#42"]
---

## Objective
One paragraph: what done means, in prose.

## Success Criteria
- [ ] Verifiable checklist items. Each gets ticked with evidence before the
      anchor closes.

## Non-Goals
- Explicit exclusions.

## Procedure
1. Numbered loop steps. Start from this skill's mode defaults; edit to
   specialize. A micro-goal procedure can be three lines: claim, plan,
   implement, and close a leaf task; if leaves remain, compact and repeat;
   if none remain, close the anchor.

## Budgets
- max_iterations: 20
- max_active_agents: 3
- max_runtime: 4h

## Stop Conditions
- All success criteria met and anchor closed -> status: done
- Any budget exceeded -> suspend, log, notify the user
- External stop signal or user cancel -> suspend

## Escalation
- Blocked on a decision: escalate_task on the blocking task, message the
  user, continue other actionable work.
- Never guess on: <listed decisions>.

## Progress Log
- 2026-07-17 #42 iter 1 — claimed #18360, closed #18360; compacted.
```

Budgets are advisory: you enforce them by checking the doc each iteration.
Hard bounds still apply underneath (`max_stop_attempts`, the global agent
slot cap, bounded waits, external stop signals). Size leaves realistically —
the loop compacts after every leaf close, so many tiny leaves means many
compactions.

## Intake → Draft

1. Resolve the anchor:
   - The objective names an existing task or epic ref → verify it with
     `gobby-tasks:get_task` and use it as the anchor.
   - A micro-goal with no existing tree → create the anchor with the user
     (an epic when the work needs multiple leaves, a task otherwise), plus
     the leaves if they are known.
   - Large undesigned scope (needs decomposition, review, coverage) → this is
     `/gobby plan` + `gobby build` territory. Say so and stop; goal does not
     replace plan or expand.
2. Draft `.gobby/goals/<slug>.md` with `status: draft`, filling the contract
   from the objective and the anchor's tree. Micro-goal fast path: the user
   pasted a complete contract → write it immediately, single confirmation.
3. Present the draft for confirmation.

## Confirm & Kickoff

On approval, set `status: active` and ask where it runs:

1. **Here** — continue into Execute below.
2. **Another session** — send the run command into its terminal:
   1. Pick the target: the ref the user named, or
      `gobby-sessions:list_sessions` filtered to active terminal sessions in
      this project, and let the user choose. Cross-project targets are
      rejected by the daemon.
   2. Compose per target source: `$gobby goal run <file>` for Codex,
      `/gobby goal run <file>` for slash-router CLIs.
   3. Send with `gobby-sessions:send_keys` in two steps — the command text
      without a newline, then a separate `Enter` — so slash autocomplete
      cannot swallow the submit.
   4. Verify with `gobby-sessions:capture_output` that the router picked it
      up. On failure, print the exact command for manual paste and offer the
      spawn option instead.
   5. If the target CLI has no gobby router installed, send the portable
      prompt from the Fallback section instead of the run command.
3. **Spawned agent** — `gobby-agents:spawn_agent` with a prompt naming the
   goal file and anchor; the spawned session runs `goal run` itself. The
   prompt must end by instructing the agent to call
   `gobby-agents:end_agent_run` after Completion — without it the run sits
   `running` until the daemon's idle watchdog reprompts it and eventually
   fails it, many minutes later and with the wrong terminal status.
4. **Taskmaster** (`--taskmaster`) — for swarm goals:
   `gobby-agents:spawn_agent` with `agent="goal-taskmaster"` and a prompt
   naming the goal file and anchor. The taskmaster template loads this
   skill itself, runs Execute setup, coordinates the swarm loop with
   routed workers, and already carries the end_agent_run and messaging
   contract — the prompt only needs the file and anchor.

## Execute — Shared Setup

1. Claim the anchor. One active goal per session: if this session already has
   an `auto_task_ref`, refuse and offer to kick off another session or spawn.
2. Set session variables:
   - `auto_task_ref` = the anchor ref (activates the autonomous loop rules)
   - `goal_file` = the goal doc path (re-injected every turn start)
   - merge `"goal"` into `additional_skills` for advisory child-session guidance.
     A successful load also records it in `loaded_skills`, making it a required
     reload after every compaction for the rest of this session.
3. Append a Progress Log entry recording this session and mode.

## Solo Loop

1. `gobby-tasks:suggest_next_task` scoped to the anchor. No candidates and
   the tree is complete → go to Completion.
2. Claim the leaf. Implement per its contract, validate, commit, close.
3. Append a Progress Log entry (iteration, leaf ref, outcome).
4. After closing a leaf while the anchor stays claimed, call
   `gobby-sessions:set_handoff` with concise current state, next steps, and
   `clear_session=false`: pass the current session ref as the top-level
   `call_tool.session_id`, not inside `arguments`. In a terminal session that call
   comes back as a rejected or cancelled tool use attributed to the user. That is
   the daemon interrupting the turn to deliver the compaction command, never a
   refusal: do not stop, do not ask the user about it, and resume from the
   continuation prompt.
5. After the compaction resume, the injected context names the goal file —
   re-read it if it is not in context, check budgets and stop conditions
   against the Progress Log, and go to step 1.

## Swarm Loop

The goal session is the coordinator. It claims only the anchor; workers claim
leaves.

1. **Sweep** — anchor tree state (`get_task`), ready leaves
   (`list_ready_tasks`), running workers (`gobby-agents:list_running_agents`).
2. **Exit** — no ready leaves, no running workers, tree complete → Completion.
3. **Capacity** — `gobby-agents:can_spawn_agent`, capped further by the doc's
   `max_active_agents` budget.
4. **Dispatch** — `suggest_next_task` for a non-conflicting batch; several →
   `gobby-agents:dispatch_batch`, one → `spawn_agent`. Route by the leaf's
   assigned agent or category defaults; override the model only when the goal
   doc names one. Every worker prompt must end by instructing the worker to
   call `gobby-agents:end_agent_run` once its leaf is closed — spawned
   interactive CLI sessions idle at their prompt afterward, holding their
   slot until the idle watchdog reprompts and then fails the run minutes
   later. Explicit end_agent_run frees the slot immediately, records
   `completed` instead of a watchdog failure, and avoids reprompting a
   finished worker into unwanted extra work.
5. **Harvest** — verify by task state; run status is advisory. For each
   dispatched leaf, `get_task` must show it closed and the work landed;
   use `get_agent_result` on runs that reached terminal status. When its
   payload includes capture metadata, page `get_agent_capture` until
   `next_offset` is null before consuming the complete advisory result. A worker
   whose leaf is verified closed but whose run is still `running` (idling
   at its prompt) is harvested — reclaim its slot with `stop_agent` rather
   than waiting on it. First failure on a leaf → respawn once with the
   failure context. Second failure → take the leaf over in this session or
   `escalate_task`. Never a third blind redispatch.
6. Work actionable coordinator items yourself (escalations, small fixes,
   Progress Log). Compact on context pressure or after finishing your own
   work item.
7. **Idle** — only when workers are running and nothing is actionable:
   subscribe once by calling `gobby-agents:wait_for_agent(run_id)`. If the run
   remains active, end the turn. On the daemon wake, re-call
   `gobby-agents:wait_for_agent(run_id)` first for the terminal snapshot, then
   run a full status and health sweep. Do not use Bash sleep loops, tmux polling
   loops, or provider monitors for worker waits.

## Resume

1. Locate the candidate goal doc: the argument, else `get_variable("goal_file")`,
   else the newest `status: active` file in `.gobby/goals/`.
2. Read the candidate and its anchor. Resume only when the document has
   `status: active` and its recorded anchor exactly matches the live anchor
   task. If this session already has `goal_file` or `auto_task_ref`, both must
   exactly match the candidate path and anchor; reject a different goal.
3. Reconcile the Progress Log against the live tree with
   `get_task` / `list_tasks` — the database wins over the log.
4. Re-claim the anchor if unclaimed, run Execute setup, append a resume entry
   with this session's ref, and enter the mode's loop.

Everything resume needs is the matching active file, matching anchor, and
database state, so any CLI can pick up the same goal another CLI started.

## Fallback — Target CLIs Without The Router

Loop enforcement is daemon-side and keyed off database state, so an executing
session does not need this skill loaded to keep a goal alive. The minimal
contract is:

1. Claim the anchor (`gobby-tasks:claim_task`).
2. Set `auto_task_ref` to the anchor ref and `goal_file` to the doc path
   (top-level `set_variable`).

Any hooked CLI that does those two things gets objective injection at turn
start and a turn-end stop-block until the tree completes — the rules fire in
the daemon whether or not the skill loaded. When kicking off into a CLI
without the gobby router, send this portable prompt instead of the run
command:

```text
Execute the goal in .gobby/goals/<slug>.md.
1. Claim <anchor> via gobby-tasks:claim_task.
2. set_variable("auto_task_ref", "<anchor>") and
   set_variable("goal_file", ".gobby/goals/<slug>.md").
3. Read the goal doc and follow its Procedure, Budgets, and Stop Conditions
   until Completion; append Progress Log entries as you go.
```

## Suspension & Stop Conditions

Suspend — on an external stop signal, user cancel, or a blown budget:

1. Set `status: suspended` and record exact state in the Progress Log (open
   leaves, running workers, blockers).
2. `gobby-sessions:set_handoff` with the same state, actionable next steps, and
   `clear_session=true`.
3. Stop workers you spawned or record their run ids as intentionally live.
4. Clear `auto_task_ref` and `goal_file`, unclaim the anchor, notify the user.

Do not close, unclaim, or move work out of the anchor just to satisfy a stop
hook, context limit, or handoff pressure. If the goal is not complete, keep the
anchor claimed and continue, compact the session, or suspend explicitly as
above. A stop hook is a reminder to finish or hand off the goal; it is not
evidence that the goal is complete. Resolve escalations yourself whenever
possible; leave a task escalated only when a user decision is genuinely
required.

## Completion

1. Tick every Success Criteria box with evidence; run required validation.
2. Verify: no workers still running, no leaf accidentally claimed, no stale
   worktrees or clones from this goal (or the exception is documented).
3. Close the anchor. Set `status: done` with a final Progress Log entry.
4. Clear `auto_task_ref` and `goal_file` — the loop rules keep firing until
   these variables are cleared.
5. Report: criteria evidence, iterations, workers used, anything suspended or
   escalated.

## Presets

Presets are starting-point goal docs for recurring loop shapes. Copy one into
`.gobby/goals/<slug>.md`, fill the placeholders, then confirm and kick off
like any other goal.

### build-coordinator

Coordinate a `gobby build` run as a goal: the coordination epic is the anchor
and the build-coordinator skill's During The Build loop is the Procedure. The
executing session must load the `build-coordinator` skill alongside this one —
it owns build semantics (startup, routing, bug policy, restart gates); the
goal doc owns the loop. Mode is `solo`: build automation dispatches its own
agents; the coordinator works coordination bugs itself, so the goal swarm
loop is not involved.

```markdown
---
goal: Coordinate gobby build <target-ref>
status: draft
anchor: "<coordination-epic-ref>"
mode: solo
created: <date>
sessions: ["<session-ref>"]
---

## Objective
Run gobby build <target-ref> to completion as coordinator: daemon-owned
automation finishes the target tree, and every gobby build bug discovered
during the run is fixed, committed, and closed under the coordination epic.

## Success Criteria
- [ ] Target work complete per its stages; no leaf in the wrong stage.
- [ ] Every discovered build bug closed with a linked commit.
- [ ] No agents running, no stray claims, no stale worktrees or clones.
- [ ] Merge stage records the real merge SHA where applicable.

## Non-Goals
- Implementing product leaves yourself — assigned agents own product work.
- Manual dispatcher ticking to keep the build moving.

## Procedure
1. Load the build-coordinator skill; run its Startup with this anchor as the
   coordination epic; launch `gobby build <target-ref> --coordinator current`.
2. Sweep per During The Build: build state, dispatch eligibility, active
   agents, build history, workspace health, open coordination bugs.
3. Work the highest-priority actionable coordination bug yourself; resume
   automation only after blocking bugs are fixed. Daemon or build-system
   fixes pass the Post-Fix Daemon Restart Gate before releasing blockers.
4. Call `set_handoff(clear_session=false)` after finishing a coordination bug or on
   context pressure.
5. Idle only when workers are running and nothing is actionable: subscribe once
   by calling wait_for_agent(run_id). If the run remains active, end the turn.
   On the daemon wake, re-call wait_for_agent(run_id) first for the terminal
   snapshot, then go to 2 for a full status and health sweep.
6. Build done: verify the build-coordinator Completion Gates, close the
   anchor, set status: done, clear variables.

## Budgets
- max_iterations: 30
- max_active_agents: <build concurrency>
- max_runtime: 8h

## Stop Conditions
- Completion Gates met and anchor closed -> status: done
- Any budget exceeded -> suspend, log, notify the user
- External stop signal or user cancel -> suspend

## Escalation
- Blocked on a decision: escalate_task on the blocking bug, message the
  user, continue other actionable work.
- Never guess on: changing the required agent, provider, lifecycle route,
  task scope, or acceptance criteria to make a build pass.

## Progress Log
- <date> <session-ref> setup — coordination epic created, build launched.
```

## Boundaries

- Do not design task trees here — decomposition, coverage, and review belong
  to `/gobby plan` and `gobby build`.
- Do not close the anchor while leaves are open or criteria are unticked.
- Do not register goal docs with the plans registry.
- Do not run a second concurrent goal in one session.
- Do not shadow a CLI-native `/goal`.
