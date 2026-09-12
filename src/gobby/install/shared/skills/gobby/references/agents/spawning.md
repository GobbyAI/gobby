# Start assigned worker runs

Load when authorized work calls for a Gobby-managed worker or batch. Inspect the
installed spawn-capable definition, assigned task, parent session, and workspace
before launching. `gobby-agents:can_spawn_agent` checks capacity/depth eligibility;
`evaluate_spawn` offers a dry run of supported launch fields. Neither reserves
capacity nor proves a later launch will succeed.

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
