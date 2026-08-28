---
name: build-coordinator
description: "Use when coordinating a full gobby build run for an epic or task, especially when the user assigns the current session as coordinator, asks for a coordination epic, wants build agents/worktrees monitored, or wants gobby build bugs fixed so future runs work unattended."
version: "1.2.0"
category: core
triggers: gobby build coordinator, epic coordinator, coordination epic, unattended build, build bugs
metadata:
  gobby:
    audience: all
    depth: 0
    format_overrides:
      autonomous: full
---

# Build Coordinator

Use this skill when the user gives a target task or epic and says the current
session is the build coordinator. The coordinator's job is broader than finishing
the target: make `gobby build` capable of running the same shape of work
unattended.

This skill is operating instructions for the current coordinator session. Do not
create or switch to a separate agent definition unless the user explicitly asks
for that.

When writing a prompt for another coordinator session, include an explicit load
directive such as `$gobby build-coordinator <target-ref>` for Codex sessions.
Use `/gobby build-coordinator <target-ref>` where the slash router is supported.

## Core Contract

Treat coordinator intervention as evidence. If `gobby build` needed the
coordinator for something it should handle itself, file a build-system bug under
the coordination epic.

Keep two work streams separate:

- Target task or epic: the user's product work.
- Coordination epic: build coordination, build-system bugs, and coordinator
  fixes discovered during the run.

Do not close the target task or epic while known `gobby build` bugs from the run
remain open.

The coordination epic is the active goal record for the run. Do not close, unclaim,
or move work out of it just to satisfy a stop hook, context limit, or handoff
pressure. If the goal is not complete, keep the coordination epic claimed and
continue, compact the session, or ask the user to explicitly cancel or pause the
goal.

## Startup

1. Load relevant local skills before acting: `tasks` and `source-control`.
Load `build` when launch
   semantics or CLI flags are in scope.
2. Identify the target task or epic and the user's non-default constraints.
   Ask only when the target is missing or a decision cannot be inferred safely.
3. Create and claim a separate coordination epic outside the target task tree.
   Put all coordinator work and all discovered build-system bugs under it.
4. Inspect the target dependency tree and stage manifests before dispatch.
   Normalize leaf task stages to the user's required stage. For implementation
   build epics, default leaf tasks to `development` unless the user says
   otherwise.
5. Check current build state and dispatch eligibility with supported MCP tools
   before launching.
6. Launch `gobby build <target> --coordinator current` without `--quick`
   unless the user explicitly requested a one-step smoke run. For target work in
   another project, use
   `gobby build <target> --project <target-project> --coordinator current`.

## Coordinator Session Routing

`--coordinator current` means the caller process `GOBBY_SESSION_ID`. For
cross-project builds launched with explicit `--project`, the CLI canonicalizes
that caller-project session ref to the caller session UUID before sending the
build request to the target project.

Explicit `--project` accepts `--coordinator current` or a full session UUID.
Project-local refs such as `#N` and bare numeric refs are rejected because they
would resolve in the target project instead of the caller project.

## During The Build

Use supported MCP surfaces first:

- `gobby-tasks` for task state, stage manifests, build status, dispatch
  explanation, and build history.
- `gobby-agents` for running agents and agent results.
- `gobby-sessions` for session metadata, terminal capture, handoff context, and
  coordinator compaction.
- `gobby-worktrees` or `gobby-clones` when workspace state is part of the
  failure.

Monitor dispatch directly with the supported MCP surfaces above. The coordinator
loop order is:

1. Check target build state, dispatch eligibility, stage state, active agents,
   build history, and workspace health.
2. Check the coordination epic's child tasks for open build bugs, claims,
   escalations, and validation failures.
3. Work the highest-priority actionable coordination bug yourself, or coordinate
   the owning session when a child bug is already claimed.
4. Resume or launch build automation only after known blocking bugs for the
   immediate dispatch path are fixed or explicitly documented as non-blocking.
5. Use `gobby-sessions:set_handoff` with `clear_session=false` when context pressure is
   high or when you have not compacted recently. Always compact after completing a coordination bug task
   before the next coordinator-loop iteration or agent wait.
6. Use `gobby-agents:wait_for_agent` as the last idle action only when agents
   are running and no actionable work remains; subscribe once by calling it for
   a specific run id. If the run remains active, end the turn.
   On the daemon wake, re-call `gobby-agents:wait_for_agent` first to retrieve
   the terminal snapshot, then run a full status and health sweep.

Do not keep the build moving by repeatedly manual-ticking the dispatcher. A
normal build is daemon-owned automation. Use resume or explicit ticks only after
recording evidence that they are diagnostics or recovery for a build bug.

## Interactive E2E Validation

For an unattended build-flow test, do not use the same task as both the tracking work and the automation target.

- Keep a separate claimed coordinator/tracking epic for the active session; use it for blocker fixes and to prevent stopping mid-run.
- Create a separate build or document epic as the `gobby build #epic` target. Fix and merge blockers before starting the final automation epic.
- Before the final launch, verify project build automation is enabled. If it is paused from prior `gobby build stop` or restart work, run `gobby build resume` once to restore daemon-owned dispatch before judging the E2E.
- Use `--quick` only for smoke checks of one lifecycle step. For real end-to-end validation, run without `--quick`, usually with bounded concurrency such as `--max-active-agents <n>`.
- For final unattended E2E, launch `gobby build #epic ...` once and then observe daemon-owned automation through task state, agent runs, logs, and final acceptance checks.
- Do not repeatedly rerun `gobby build` as manual dispatcher ticks to keep work moving. That is an anti-pattern because it changes the test shape and can hide a broken dispatcher loop.
- Be explicit about docs scope. For `docs/guides` refreshes, leave root `README.md` out unless it is named in scope; run `docs/guides/README.md` last when it indexes the other guides.
- Do not make the test pass by changing the required agent, provider, lifecycle route, task scope, or acceptance criteria. Diagnose and fix the blocker while preserving the requested path.
- Escalating because an assigned agent is incapable of the task is an extreme edge case. Use it only after exhausting practical fixes and documenting why the specified agent path cannot complete the work.
- Final acceptance must verify that the document epic is closed, the merge stage records the real merge SHA, no agents are running, no tasks remain claimed for the build, no stale build worktrees or clones remain, and any intentionally preserved dirty or conflicted workspace is explained.

## Automation Debugging Pattern

When build automation stalls or agents behave as if required startup context is missing, debug the lifecycle rather than changing requirements.

- Compare against the last known successful run before changing configuration. Check what changed in agent definitions, provider routing, rules, hooks, MCP startup config, and step workflows.
- Treat provider-specific symptoms as evidence, not proof of provider incapability. Preserve the requested agent/provider path while diagnosing.
- If progress appears stuck after the initial launch, first prove daemon-owned dispatch is idle or wedged by checking task stages, active agent runs, automation loop health, and correlated logs. If project automation is paused, use `gobby build resume`; a bounded explicit tick may be used only as a diagnostic or recovery step after recording evidence, not as normal E2E execution.
- Check whether SessionStart activation completed and left expected state before the first provider-neutral prompt event: session linkage, `_agent_type`, active rule and skill variables, terminal pickup metadata, baseline dirty-file capture, and any spawned-agent step workflow.
- A first-prompt reconciliation guard can be useful as a backstop: call an idempotent `ensure_session_activation(session_id)` helper that creates only missing activation effects and preserves existing progress. Do not replay the raw SessionStart hook wholesale.
- For cross-boundary stalls, use OpenTelemetry or equivalent correlated logs keyed by `agent_run_id` and `session_id` across spawn, tmux, SessionStart, workflow activation, auto-claim, step transitions, MCP calls, and rule blocks.

## Post-Fix Daemon Restart Gate

When you commit a daemon or build-system fix that changes dispatch, spawn, build
controls, stage transitions, session handoff, worktree or clone isolation, or
agent startup behavior, assume the running daemon still has the old code until
you prove otherwise.

Before releasing blockers, resuming builds, or allowing future dispatch through
the affected path:

1. Stop or keep blocked the affected build targets under the relevant child bug.
2. Stop agents already spawned through the stale behavior and record their run
   IDs, task refs, workspace paths, and isolation metadata.
3. Restart the daemon after notifying active agents and giving them a short head
   start when active agents exist.
4. Verify daemon health, call `gobby-sessions:set_handoff` with structured state and
   `clear_session=false`, and run another
   full status sweep.
5. Confirm the next eligible spawned agent uses the expected isolation and
   workspace metadata before treating the fix as effective.

If an agent launches after the fix with old workspace or isolation metadata,
file or keep open a child build bug for stale daemon behavior. Fix or document
the automation gap before continuing the target build.

If a stop hook fires while the coordination epic is still claimed, continue the
coordinator loop above. A claimed coordination epic means the build goal is still
active; finish actionable child work, monitor agents, or hand off with saved
context.

Do not close the coordination epic to clear a stop hook.

Resolve escalations yourself whenever possible. Leave a task escalated only
when a user decision is genuinely required.

## Compaction

Compact at handoff boundaries and before context size starts degrading
decisions. If `gobby-sessions:set_handoff` is not leased in the current context,
call `get_tool_schema(server_name="gobby-sessions", tool_name="set_handoff")`
directly. Then call `set_handoff` with concise `current_state`, actionable
`next_steps`, and `clear_session=false`.

Pass the current Gobby session ref as the top-level `call_tool.session_id`, not
inside `arguments`.
requires attribution.

In a terminal session that call comes back as a rejected or cancelled tool use
attributed to the user. That is the daemon interrupting the turn to deliver the
compaction command, never a refusal: do not stop, do not ask the user about it,
and resume from the continuation prompt.

Before compacting, leave enough state in the conversation or task notes for the
continuation to resume without rediscovery.

## Build Bug Policy

File every discovered `gobby build` bug under the coordination epic. Examples:

- dispatch stalls, missing cron resume, or daemon-owned automation stops
- bad stage routing, incorrect stage manifests, or impossible stage transitions
- wrong target branch, integration branch, worktree, or clone setup
- agent launch missing required startup context, skills, task links, or claims
- merge, validation, close, or cleanup behavior that requires manual repair
- missing build history or status visibility that prevents safe unattended
  operation

Fix blocking bugs immediately. Fix non-blocking bugs when agents are running or
other coordinator work is idle. All discovered unattended-build bugs must be fixed,
committed, linked, and closed before the target task or epic is closed.

Product-task failures are different from build-system bugs. Let assigned agents
own product work unless the failure exposes broken build automation or the user
explicitly asks the coordinator to take over.

## Context And Handoff

When context gets large, compact before degradation affects decisions. Include:

- target task or epic ref
- coordination epic ref
- exact build command and important flags
- current build status and active agent runs
- discovered build bugs, fixed commits, and remaining work
- escalations that still need user decisions
- validation already run

## Completion Gates

These gates apply to the coordination epic itself as well as the target. A stop
hook is a reminder to finish or hand off the goal; it is not evidence that the
goal is complete.

Before closing the target task or epic, verify:

- target product work is merged or otherwise completed according to its stages
- no target leaf remains in the wrong stage
- no agents for the build are still running
- no task remains claimed accidentally
- all discovered `gobby build` bugs from the run are closed with linked commits
- required focused validation has run; do not run the full pytest suite unless
  explicitly requested
- stale worktrees or clones are cleaned up, or any intentionally preserved
  workspace is documented

Close the coordination epic only after the target is complete and every
discovered build bug from the run is fixed or explicitly blocked on a user
decision. Keep discovered build bugs under the coordination epic while they are
open; do not detach or reparent them to make the coordinator task closable.
