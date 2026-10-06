# Resolve pipeline approvals

Load when authoring an approval gate or handling a waiting execution. Inspect
`get_pipeline_status(execution_id=...)` and the concrete gated effects. A workflow
request is not blanket permission to approve unrelated side effects.

Place `approval: {required: true, message: ..., timeout_seconds: ...}` on the step
before its side effects. Persist the execution ID; read the returned approval
token from that run. `gobby-workflows:approve_pipeline(token=...)` consumes the
single-use token and continues execution, including the gated step. It may return
another approval wait; inspect the new status/token. Apply only approval already
granted by the user or owning policy. The recorded approver is your authenticated
caller identity, `session:<id>` or `operator`; no argument sets it.

`reject_pipeline(token=...)` marks the step failed and execution cancelled, and
records the rejecter the same way. Configured approval expiry has the same state
outcome through daemon maintenance, normally a 60-second sweep. No timeout means
no configured expiry.

Approval resumes the captured definition when present. Editing an installed
pipeline does not rewrite a paused run. A stale/already-used token requires
inspection of the existing run; never manufacture a replacement. Public failed
resume does not apply to waiting, rejected or expired/cancelled executions.

Operator CLI equivalents are `gobby pipelines approve TOKEN` and
`gobby pipelines reject TOKEN`; HTTP routes also accept tokens. Tokens are
sensitive approval capabilities: keep them with the authorized review context.
If later execution fails, load recovery and review performed effects before a
fresh attempt. Approval itself does not guarantee downstream success.

Verified guide: [pipelines.md](../../../../../../../../docs/guides/pipelines.md#approval).

_Last verified: 2026-10-06_
