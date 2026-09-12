# Monitoring and diagnosing builds

Load when checking build progress, eligibility, history, or a suspected stall.
Fetch the current schemas for `gobby-tasks:get_build_status`,
`explain_dispatch`, and `list_build_history`. Status/history take `input_ref`;
dispatch explanation takes `task_id`. Resolve the project explicitly when needed.
These are read-only diagnostics; a status call does not keep automation alive.

Inspect the root and affected tree, current stages, active runs, lease state,
artifact/workspace health, and recent control/dispatch events. History is bounded
by tool limits and best-effort; missing history alone does not prove no action
occurred. Follow supported pagination/limits instead of treating a first page
as a full inventory. Compare explanations with the actual manifest and claim,
dependency, escalation, closure, and project-automation state.

The daemon re-evaluates eligible tasks on heartbeat; there is no persistent
pending-agent queue. The compiled default agent cap is 20, but read configured
capacity before explaining current saturation. A launch's `max_active_agents`
overrides its immediate heartbeat only. Wait for relevant agents/events when
idle; do not repeatedly poll status or capture output as a coordinator loop.

If startup context appears absent, compare a known successful run and correlate
`agent_run_id`/`session_id` through spawn, terminal startup, SessionStart,
activation, workflow state, claim, MCP, and rule blocks. Inspect linkage,
`_agent_type`, rule/skill variables, terminal pickup, baseline dirty-file capture,
and immutable step-workflow state. Preserve the requested provider path.
Do not replay raw SessionStart or mutate tracking variables to simulate startup.

Use sessions/agents capabilities for transcripts, structured handoffs, runtime
results, and terminal diagnostics; source-control owns workspace diagnosis.
If automation is paused, wedged, or has stale isolation metadata, capture the
evidence and load recovery before changing state. An explicit tick is a bounded
diagnostic/recovery action, never proof of autonomous progress.

Guide: [Build state](../../../../../../../../docs/guides/dispatch.md#build-state).

_Last verified: 2026-09-12_
