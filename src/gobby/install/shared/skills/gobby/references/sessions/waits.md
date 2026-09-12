# Wait for work and messages

Load when progress depends on an agent, terminal condition, message, or pipeline.
Discover the applicable `wait_for_*` tool on the service owning that operation;
there is no `gobby-sessions` wait tool. Fetch its schema before calling.

For an agent run, use `gobby-agents:wait_for_agent(run_id=...)` once. A completed
result can be handled immediately; otherwise successful notification registration
means yield the turn and let completion wake the session. The tool follows daemon
resume chains. Missing context, unavailable services, or subscription failures
are errors, not successful waits. Diagnose the returned failure instead of
polling status or registering repeatedly.

For a bounded terminal condition on an agent run, use
`gobby-agents:wait_for_output` with a safe regex and bounded timeout. This tool
checks output internally; it is not a durable completion subscription. Inspect
`matched`, timeout, terminal, pane-lost, and capture-failure outcomes. A pattern
match is not proof the task completed. Do not build a loop of calls or repeated
`capture_output` snapshots around it.

Use `gobby-agents:send_message` for coordination. Message text has no wake
semantics; `wake=true` explicitly requests immediate processing. Protected
interrupted/input/approval/handoff states keep the durable message queued until
wake is safe. Use one targetless project broadcast for repository coordination,
and global only for machine-local cross-project coordination. Wait on the
appropriate primitive and yield; sleeps and repeated captures are reserved for
bounded diagnostics.

Guide: [Event-driven waits](../../../../../../../../docs/guides/sessions.md#event-driven-waits).

_Last verified: 2026-09-12_
