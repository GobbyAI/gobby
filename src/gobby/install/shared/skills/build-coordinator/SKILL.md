---
name: build-coordinator
description: "Use when coordinating a full gobby build run for an epic or task, especially when the user assigns the current session as coordinator, asks for a coordination epic, wants build agents/worktrees monitored, or wants gobby build bugs fixed so future runs work unattended."
version: "1.0.2"
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

The coordination epic is the active goal record for the run. Do not close,
unclaim, or move work out of it just to satisfy a stop hook, context limit, or
handoff pressure. If the goal is not complete, keep the coordination epic
claimed and continue, compact the session, or ask the user to explicitly cancel
or pause the goal.

## Startup

1. Load relevant local skills before acting: `task-creation`,
   `task-transitions`, and `source-control`. Load `build` when launch
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
6. Launch `gobby build <target>` without `--quick` unless the user explicitly
   requested a one-step smoke run.

## During The Build

Use supported MCP surfaces first:

- `gobby-tasks` for task state, stage manifests, build status, dispatch
  explanation, and build history.
- `gobby-agents` for running agents and agent results.
- `gobby-sessions` for session metadata, terminal capture, handoff context, and
  coordinator compaction.
- `gobby-worktrees` or `gobby-clones` when workspace state is part of the
  failure.

Monitor agents directly with the supported MCP surfaces above. Do not require a
separate monitoring skill for coordinator runs.

Do not keep the build moving by repeatedly manual-ticking the dispatcher. A
normal build is daemon-owned automation. Use resume or explicit ticks only after
recording evidence that they are diagnostics or recovery for a build bug.

Prefer `gobby-sessions:compact_self` over `wait_for_agent` when agents are
running and no useful coordinator work is available. Use `wait_for_agent` only
for a short bounded wait when you have a specific run and need its terminal
result. If useful work exists, fix discovered build bugs or improve
observability needed for this run.

Resolve escalations yourself whenever possible. Leave a task escalated only
when a user decision is genuinely required.

## Compaction

Compact at handoff boundaries and before context size starts degrading
decisions. Use progressive discovery, then call `gobby-sessions:compact_self`:

1. `list_mcp_servers()`
2. `list_tools(server_name="gobby-sessions")`
3. `get_tool_schema(server_name="gobby-sessions", tool_name="compact_self")`
4. `call_tool("gobby-sessions", "compact_self", {"session_id": "<current-session>"})`

Use the Gobby session ref for `session_id`, such as `#5812`. Set `rule_name`
only when a workflow or rule specifically requires attribution.

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

## Goal Prompt Template

When the user asks for a reusable `/goal` prompt, adapt this:

```text
$gobby build-coordinator <target-ref>
/goal Act as the Gobby build coordinator for <target-ref>.

Create a separate coordination epic outside <target-ref>. Use it for coordinator
work and every gobby build bug discovered during this run.

Run gobby build for <target-ref> without --quick. Coordinate agents and
worktrees, but treat coordinator intervention as evidence of unattended-build
gaps. File every discovered build bug under the coordination epic. Fix blocking
build bugs immediately, fix non-blocking build bugs when opportunity arises, and
fix all discovered build bugs before closing <target-ref>.

Before dispatch, inspect the target task tree and normalize leaf task stages to
the required stage. Default implementation leaves to development unless the user
specified another stage.

Prefer gobby-sessions:compact_self over wait_for_agent when agents are running
and no useful coordinator work exists. Otherwise fix discovered build bugs or
improve observability. Resolve escalations yourself unless a genuine user
decision is required.

Completion requires the target work complete, all discovered build bugs fixed and
closed with linked commits, required validation run, no unexpected running agents
or claimed tasks, and final status covering build state, validation, bug fixes,
escalations, and task states. Do not close the coordination epic to clear a stop
hook while any completion requirement remains unmet; compact or continue the
session instead.
```
