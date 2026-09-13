# Health

Load for startup failure, an unreachable client, degraded services, or disputed
readiness. Start with read-only operator commands `uv run gobby health` and
`uv run gobby status`; use domain MCP reports for agent-facing state.

1. Identify the process, singleton owner, configured ports, and local versus
   remote datastore mode. A maintenance owner is not a running daemon.
2. Read public `/api/health` and `/api/admin/startup-progress`. HTTP success
   alone is insufficient: inspect health status and startup completion.
   The CLI returns failure for a degraded hook runtime even when HTTP is 200.
3. Read authenticated `/api/admin/status` for subsystem details. Keep partial,
   timeout, unavailable, and degraded results in the diagnosis; they do not
   mean zero sessions, zero tasks, or a healthy empty service.
4. Distinguish authentication failure (401), startup/shutdown admission, schema
   divergence, an unavailable datastore, and a failed provider connection.
   Load `authentication.md`, `recovery.md`, or the relevant domain reference.
5. Capture the timestamp, failing command, target scope, and bounded matching
   logs. Repair the responsible condition, then repeat the original check.

Health checks do not authorize a restart, credential rotation, destructive
cleanup, or schema mutation. Use `daemon.md` for lifecycle coordination.
Metrics, capacity, token reports, local traces, and log collection belong to
the observability capability. Do not send users to removed dashboard or trace
navigation routes to establish daemon health.

See [diagnostics](../../../../../../../../docs/guides/admin-operations.md#setup-and-diagnostics)
and [observability](../../../../../../../../docs/guides/observability.md).
