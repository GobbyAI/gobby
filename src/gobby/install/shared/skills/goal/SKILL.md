---
name: goal
description: "Author and execute durable goal loops: intake -> draft .gobby/goals/<slug>.md -> confirm -> execute in solo or swarm mode against an anchor task. Use for /gobby goal, goal run/resume/status, autonomous epic burn-down, and compact-and-continue loops."
version: "1.0.0"
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
   `gobby-agents:end_agent_run` after Completion, or its run never reaches
   terminal status.

## Execute — Shared Setup

1. Claim the anchor. One active goal per session: if this session already has
   an `auto_task_ref`, refuse and offer to kick off another session or spawn.
2. Set session variables:
   - `auto_task_ref` = the anchor ref (activates the autonomous loop rules)
   - `goal_file` = the goal doc path (re-injected every turn start)
   - merge `"goal"` into `additional_skills` (reloads this skill after every
     compaction)
3. Append a Progress Log entry recording this session and mode.

## Solo Loop

1. `gobby-tasks:suggest_next_task` scoped to the anchor. No candidates and
   the tree is complete → go to Completion.
2. Claim the leaf. Implement per its contract, validate, commit, close.
3. Append a Progress Log entry (iteration, leaf ref, outcome).
4. Closing a leaf while the anchor stays claimed triggers automatic
   compaction. If it did not fire, call `gobby-sessions:compact_self`
   directly: pass the current session ref as the top-level
   `call_tool.session_id`, not inside `arguments`. In terminal sessions,
   compact_self interrupts the active turn before sending `/compact` or
   `/compress`; a rejected or cancelled tool-use immediately followed by that
   slash command is expected self-compaction delivery, not user refusal.
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
   interactive CLI sessions idle at their prompt afterward and never reach
   terminal run status on their own.
5. **Harvest** — verify by task state; run status is advisory. For each
   dispatched leaf, `get_task` must show it closed and the work landed;
   use `get_agent_result` on runs that reached terminal status. A worker
   whose leaf is verified closed but whose run is still `running` (idling
   at its prompt) is harvested — reclaim its slot with `stop_agent` rather
   than waiting on it. First failure on a leaf → respawn once with the
   failure context. Second failure → take the leaf over in this session or
   `escalate_task`. Never a third blind redispatch.
6. Work actionable coordinator items yourself (escalations, small fixes,
   Progress Log). Compact on context pressure or after finishing your own
   work item.
7. **Idle** — only when workers are running and nothing is actionable:
   `gobby-agents:wait_for_agent` for a specific run with a bounded five-minute
   wait (`timeout_seconds=300`), then a full sweep. Do not use Bash sleep
   loops, tmux polling loops, or provider monitors for worker waits.

## Resume

1. Locate the goal doc: the argument, else `get_variable("goal_file")`, else
   the newest `status: active` file in `.gobby/goals/`.
2. Read it. Reconcile the Progress Log against the live tree with
   `get_task` / `list_tasks` — the database wins over the log.
3. Re-claim the anchor if unclaimed, run Execute setup, append a resume entry
   with this session's ref, and enter the mode's loop.

Everything resume needs is the file plus database state, so any CLI can pick
up a goal another CLI started.

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
2. `gobby-sessions:set_handoff_context` with the same state.
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

## Boundaries

- Do not design task trees here — decomposition, coverage, and review belong
  to `/gobby plan` and `gobby build`.
- Do not close the anchor while leaves are open or criteria are unticked.
- Do not register goal docs with the plans registry.
- Do not run a second concurrent goal in one session.
- Do not shadow a CLI-native `/goal`.
