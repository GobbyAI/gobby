# Start assigned worker runs

## Per-launch network profile

Single managed launches accept MCP `spawn_agent.network` or HTTP
`POST /api/agents/spawn`'s `network` as `none`, `trusted`, or null. The CLI uses
`gobby agents spawn "Work" --session <ref> --network none|trusted` and sends the
field only when supplied. Omission/null inherits the final definition after
fallback; an explicit value applies to a copy for that launch. Stored definitions
and later launches retain their original profile; resume keeps the saved
effective sandbox configuration. `trusted` remains an SRT allowlist.

Root sessions and the local operator may override. Spawned callers need a
verified `default` or `orchestrator` run; all others use inheritance. Pipeline
MCP steps enforce the same boundary. Daemon-internal callers cannot override.
Spawn scope and seat-spawn policy remain in force. Direct HTTP spawn accepts
operator credentials only and refuses explicit network for `web_chat`.

Load when authorized work calls for a Gobby-managed worker or batch. Inspect the
installed spawn-capable definition, assigned task, parent session, and workspace
before launching. `gobby-agents:can_spawn_agent` checks capacity/depth eligibility;
`evaluate_spawn` offers a dry run of supported launch fields, including `model`.
Neither reserves capacity nor proves a later launch will succeed. Supplying
`model` requires an explicit `provider`; an incompatible pair is rejected
before any terminal or worktree is created.

Fetch `spawn_agent`'s schema. Supply a bounded prompt with deliverable, paths,
constraints, and validation; use the existing task and explicit parent/workspace
when needed. Retain `run_id`, child session, task, and isolation metadata from the
result. Task claim, preflight, and launch are separate failure points. A returned
run is not proof the worker finished. Inspect the result before retrying an
indeterminate response; avoid duplicate workers for the same task.

`dispatch_batch` sends task suggestions through the same spawn machinery. Inspect
every item for partial failures instead of retrying the whole batch blindly.
Use `wait_for_agent` once for a live run and yield; read final evidence on wake.
The lifecycle reference covers output and cancellation.

A runbook's first step is `check_runbook_seats(workspace, requested, catalogue)`,
called from its pipeline `mcp` step. It is read-only. It refuses only when the
same runbook is still launching for the same project in the same workspace on
the same machine, or when a seat's agent definition is missing or disabled.
Runbooks are fire and forget and enforce no agent slots. Seats are told apart by
`project#session_ref`, so placement never refuses a pane title already in use.

Runtime isolation accepts `none`, `worktree`, or `clone`; `inherit` belongs to
agent definitions. Existing isolation IDs request reuse. Boot-failure cleanup is
opt-in for freshly created isolation; preserve work when inspecting a failed run.
Closed-task spawning is an explicit read-only review exception using
`allow_closed_task`; it does not auto-claim the closed task. Spawning is not
permission to exceed the user's delegation scope or the configured depth limit.

CLI `gobby agents spawn` and HTTP spawn/batch/prompt-preview are operator/client
surfaces with their own request fields. Launch defaults are read through HTTP;
use configuration management for changes. Never launch provider binaries as an
unmanaged replacement for a denied managed spawn.

Guide: [Runtime tools](../../../../../../../../docs/guides/agents.md#runtime-tools).

_Last verified: 2026-09-12_
