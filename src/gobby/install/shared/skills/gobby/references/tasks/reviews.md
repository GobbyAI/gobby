# Stages and review

Load for stage manifests, autonomous review handoffs, approvals, rejection, or
stage-definition maintenance. Start with `gobby-tasks:get_task_stages`,
`list_stages_registry`, and `get_task_type_defaults`. Read installed rows before
describing stages, profiles, or workflows as active; bundled files are templates.

Stage actions live on `gobby-tasks-ops`: `initialize_task_manifest`, `start_stage`,
`complete_stage`, `fail_stage`, `submit_for_review`, `approve_review`,
`reject_review`, `add_stage`, and `remove_stage`. Use the exact current stage name
and schema. Do not mutate projected task state with metadata updates.

An ordered manifest's first unfinished row is current. Normal progression is
`ready` to `in_progress`, then either completion or required review:
`needs_review` to `review_approved` to `done`. Rejected work returns for another
attempt; failure caps can escalate it. Future ready rows can be inserted or
removed subject to lifecycle checks. A task close is a separate operation.

An autonomous implementer commits and submits its stage with concrete evidence.
The reviewer checks the stage's policy, task criteria, source changes, tests, and
TDD requirements before approval or rejection. Preserve review notes and bounded
round evidence. Stage submission auto-links relevant session commits and checks
scope; it is not interchangeable with the full interactive close checklist.
Interactive work uses the closing topic unless explicitly operating an authorized
review workflow. Honor caller-role and ownership errors instead of impersonating
a reviewer or validator.

Registry maintenance uses `update_stage`, `restore_stage`, `delete_stage`, and
`set_task_type_defaults` on `gobby-tasks-ops`. Inspect installed definitions and
usage before changing shared defaults. Deletion is restricted by usage; use the
reported blocker and preserve user-owned overrides. These operations change
workflow configuration, not completion evidence.

For planning-review evidence use `$gobby plan`; for epic QA use `$gobby review`;
for PR/merge review delivery use `$gobby source-control`.

Guide: [Stage Manifests](../../../../../../../../docs/guides/tasks.md#stage-manifests).

_Last verified: 2026-09-12_
