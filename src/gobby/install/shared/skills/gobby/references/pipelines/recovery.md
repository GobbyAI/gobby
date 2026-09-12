# Recover pipeline work

Load when a run fails, stalls, is cancelled, or crosses daemon restart. Start
with `get_pipeline_status` and complete step evidence; use history discovery if
the execution ID is unknown. Diagnose the specific step and child run first.

For `failed`, fix the underlying cause and inspect the current installed
definition against prior effects. `gobby-workflows:resume_pipeline(execution_id,
from_step=...)` resets that step and following steps to pending using stored
inputs and the current enabled definition. Without `from_step`, it selects the
first failed/errored step. Earlier outputs survive. A losing concurrent claim
must inspect the existing resumed run. The caller is subscribed: yield afterward.

Public resume rejects other statuses, including interrupted and cancelled.
Do not change DB status to force eligibility. Reconcile side effects before
an authorized fresh run; idempotency is required wherever a step can repeat.

`resume_on_restart: true` independently opts enabled definitions into recovery
of running executions. Completed/skipped steps are retained; unfinished work can
repeat. Non-recovered running executions become interrupted. Native Ask delegates
restart handling to its own service; follow code-index Ask guidance for those runs.

`cancel_pipeline(execution_id=...)` cancels the tracked background task and
attempts to kill agents owned by the pipeline child session. It does not undo
completed operations. Inspect remaining children, external processes and effects
before declaring cleanup complete. Keep failed output for diagnosis.

For operator CLI HTTP read timeout, the daemon may still be executing; inspect
history rather than starting again. Restart the shared daemon only through the
coordinated lifecycle procedure. For cron failures, inspect linked child status,
overlap, readiness and capacity; changing a schedule does not repair child work.

Verified guide: [pipelines.md](../../../../../../../../docs/guides/pipelines.md#resume).

_Last verified: 2026-09-12_
