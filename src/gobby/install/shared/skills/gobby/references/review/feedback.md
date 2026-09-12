# Feedback batches

Load before capturing Gobby feedback or reviewing a frozen observation batch.
Discover `gobby-sessions:feedback` for capture and the three `gobby-feedback`
tools for batch evidence. Lease schemas first; read
[outcomes](outcomes.md) before interpreting results as completed work.

## Capture

`gobby-sessions:feedback` atomically stores structured observations for the current
session. An empty observations list records that the epoch was considered; it
does not invent feedback. Follow the current observation schema and actual task
disposition requirements. Feedback is not a substitute for fixing found work or
handing it to its active owner. Do not duplicate an acknowledged epoch merely
because context was compacted.

## Review the assigned batch

Read `gobby-feedback:get_review_observations(run_id="<assigned-run-id>", offset=0,
limit=50)` and follow every returned `next_offset` until null. Offsets are
nonnegative and limits are 1–100. New feedback cannot enter this frozen batch.
`historical_inputs_missing=true` means frozen input evidence is absent; do not
invent observations or infer their contents from a digest.

Cluster each frozen observation exactly once. Use the current findings contract
from the assigned review prompt: required cluster fields are observation IDs,
cited paths, theme, classification and digest note; proposals require title,
description and nonblank verification evidence. Classifications are `defect`,
`guidance-gap`, `noise` and `praise`. Propose work only for currently verified
actionable defects/guidance gaps, within the assigned task budget. Copy cited
repository paths from observations; do not invent paths or IDs.

Check current code, commits, installed configuration and existing open work.
Old dispositions, later commits and closed tasks are investigation leads, not
proof of resolution. Explain uncertainty rather than filing an unverified defect.

Read the report destination supplied by the reviewer launch. Revise one cumulative
daily synthesis, preserving earlier verified findings, resolutions, task refs and
uncertainties. Findings JSON covers only this frozen batch. Omit the generated
section beginning `<!-- gobby-feedback-outcomes -->` from `summary_md`.

Submit `gobby-feedback:submit_review(run_id="<assigned-run-id>", findings=<verified
batch>, summary_md=<daily synthesis>)`. Only the assigned reviewer session of a
running review may submit. The successful response names the report path; the
submission is durable independently of the agent's later termination. Then end
the agent run with short status and report reference, never the findings JSON or
report body in a handoff.

## Failures and operator boundary

Repair rejected coverage, malformed findings or blank summary before resubmitting.
A wrong reviewer or non-running review error is an ownership/state problem;
do not spoof the assigned session or write directly to storage. Inspect accepted
results and coordinate with the run owner.

The operator can trigger `gobby feedback review [--dry-run]` or
`POST /api/feedback/review`. Dry runs still create review/report evidence but file
no tasks and mark no observations reviewed; they are not read-only probes. Test
these examples only with isolated state. CLI readers are `feedback observations`,
`feedback results` and `feedback digest`; details are in the
[CLI guide](../../../../../../../../docs/guides/cli-commands.md#feedback-review).
