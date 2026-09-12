# Schedule and maintain cron jobs

Load before creating, editing, running or diagnosing schedules. Discover
`gobby-cron:list_cron_jobs(project_id=...)`, then `get_cron_job(job_id=...)`.
Use UUIDs in MCP/HTTP calls; operator CLI also resolves names.

`create_cron_job` accepts cron/interval/once schedules and agent_spawn/pipeline/shell
actions. Explicit project scope is preferred; default is caller project, then
personal project. Cron uses `cron_expr`, interval `interval_seconds`, once a future
ISO `run_at`. Set IANA `timezone` for calendar intent; default is host timezone.
Intervals below 60 seconds are clamped. Prefer five-field calendar expressions.

Creation defaults to enabled and has no enabled argument. Verify a harmless
future-scheduled action in isolated fixtures first. For existing user jobs, use
`update_cron_job(enabled=false)` before changing side effects, explicitly test
once with `run_cron_job`, inspect outcome, then re-enable. Update replaces
`action_config`; preserve required keys. MCP update does not accept `run_at`;
operator HTTP supports one-shot rescheduling. Use resolved task UUIDs in updated
pipeline inputs; only creation resolves short `inputs.task_id` references.

| Action | Configuration |
| --- | --- |
| pipeline | `pipeline_name`, optional `inputs` |
| shell | Executable `command`, separate `args` array, optional `cwd` and timeout |
| agent_spawn | Required `prompt`; provider/timeout/workflow/agent_definition subset |

Cron shell does no splitting or implicit shell; default timeout is 60 seconds.
Agent default provider is claude and timeout 300 seconds; agent_definition adds
prompt/provider selection, not arbitrary spawn controls. Use pipeline MCP spawn
for richer controls. Outer action budget also applies. Multi-step work belongs
in a pipeline; task dispatch belongs in build.

Manual run bypasses schedule/enabled state but obeys admission/capacity and
retired-job checks. It returns admission, not completion. `dispatched` links a
child run; follow that result. Default child overlap is skip_if_active; disabled
target pipelines are skipped. Only failures increment backoff. Diagnose rejection
codes and child readiness rather than repeatedly starting work.

`toggle_cron_job` flips state; prefer explicit update for a known target state.
`delete_cron_job` removes user jobs and history. System rows reject ordinary edits,
toggles/deletion except allowed display metadata; operators use cron park/wake
for scheduling. Park does not cancel active work. Check installed is_system and
enabled values, not bundled source. Restart-protected jobs use coordinated daemon
lifecycle. Never mutate the user's live state as a documentation test.

Verified guide: [cron-scheduler.md](../../../../../../../../docs/guides/cron-scheduler.md).

_Last verified: 2026-09-12_
