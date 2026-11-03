# Close verified work

Load before task closure, including a disposition with no implementation work.
Discover `record_attestation`, `list_attestations` and `close_task` on
`gobby-tasks`; schemas own their parameters. The enabled definition's selected
rules choose the evidence and conditions for closing. There is no fixed Python
checklist, category-to-step mapping, role roster or close-reason enum.

## Complete the selected workflow

1. Finish the implementation and acceptance criteria. Resolve owned findings.
2. Perform the validation, testing and review required by the selected workflow.
   Record exact commands, results, limitations and nonapplicability truthfully.
   Follow project test-quality, type and suppression checks for changed tests.
3. Record evidence with `record_attestation(task_id, kind, result, summary, ...)`.
   Its optional reference is a scheme/value pair, such as a Git SHA or a document
   identifier. JSON evidence can carry criteria and validation results. The server
   stamps the authenticated author, installed definition, capabilities and time.
4. For repository work, commit owned paths with the task reference. Preserve
   foreign edits. Link the candidate and land it through the definition's declared
   integration authority. A landing receipt does not close the task.
5. Record any final evidence required for that exact candidate. Preview
   `close_task(task_id=..., commit_sha=..., changes_summary=..., preview=true)`.
   For non-Git work, omit the commit and repository parameters.
6. Resolve every policy refusal. Use the returned `snapshot_revision` as
   `expected_closure_revision` on the final call when binding it to that preview.
   A changed task, policy or evidence may require another preview.
7. After `closed=true`, review task memories and personally remove the clean,
   merged task worktree and branch when the workflow calls for cleanup.

Use `link_commit` when attaching work without closing. An explicit candidate can
also be linked atomically with the final close; preview leaves links unchanged.
A close reason is nonblank text, at most 120 characters. Selected YAML determines
which reasons need disposition evidence, references or other requirements.

## Evidence and policy

Kinds declare allowed_results and guidance in task_attestation_kinds. The server refuses other results with that guidance.
A selected closure rule may declare an `attestation_shape` (kinds, result, reference scheme and
value pattern, task applicability). Recording refuses evidence that shape can never accept and
names the accepted shape. A result the author's definition declares for the kind in
task_attestation_kinds stays recordable, such as `rejected`; a definition that declares no kinds
can record only the shape's result for shaped kinds. The engine does not interpret
an `implementation`, `test`, `independent-review` or `closing` record as special authority.
The selected `task_close` and `task_land` rules inspect the task, authenticated
caller, repository facts and append-only attestation history. Rules can require
ordered records, exact references, approved results, independent authors or
particular author capabilities. They can also operate without a Git repository.

`task_attestation_writes` grants kinds (or `*`); `task_attestation_scope` limits
the tasks an author can attest. Authors correct their own evidence with
`supersedes_id`, preserving history. An idempotency key permits an exact replay;
it does not allow overwriting evidence. Never author a peer's attestation.

The bundled coding policy requires TDD evidence, implementation, validation,
OCR self-review, a linked and integrated candidate, clean attributed files and
closing evidence. Follow its current rules for ordering and candidate references.
The paired workflow additionally requires independent review. Task-specific
security approval remains independent and capability-bound. A later rejection
or correction can revoke approval for a subsequent landing or close attempt.
These are YAML policies, not task-engine assumptions about all tasks.

Default can supply its own required evidence, land through its path grant and
close without a manager or a general independent reviewer. The DevTeam lead owns landing, post-land validation, closure and cleanup; developers and reviewers hand completion to it through the pipeline claim_flow. A definition's `task_close_scope` and
`task_land_scope` constrain task access. `respect_task_protections` preserves the
project's protected labels, subtrees and foreign claims. Preview applies the
same access checks as final close and changes nothing.

## Landing and atomic decisions

`gobby-tasks-ops:land_commit` requires an enabled definition with `land_tasks`,
local ownership and the declared task scope. It evaluates selected `task_land`
rules and preserves Git path, conflict, freeze, schema-carrier and receipt
integrity. A task-bound team manager or a particular definition name is not
required. Default's integration-path grant covers its self-delivery workflow.
Raw checkout-changing Git merges remain guarded.

A final close captures task, policy, author and evidence under database locks,
evaluates that snapshot and writes the close atomically. Ancestors are evaluated
under their own policy; a refused ancestor does not undo a completed leaf.
A landing request records its admission snapshot in its receipt; changed evidence
is evaluated on the next request. Code owns integrity and authenticated
provenance. Definitions, rules and pipelines own completion standards.

Follow the assigned restart and activation owner. Announce a daemon restart or
cutover globally, honor quiet hours, proceed, and report DAEMON BACK when healthy.
Separately coordinate operations that can kill agent panes.

Guide: [Close](../../../../../../../../docs/guides/tasks.md#close).

## DevTeam validation

The daemon moves claims in record_attestation's transaction from the pipeline's
implement stage to review, back on rejected independent-review, then to validate.
One validation kind carries stage and criteria [{criterion, met, how}], judged
against task.validation_criteria. The lead validates the integrated SHA after
landing before recording closing. Superseded records remain in all_attestations;
policy reads the active set. Developers never land, merge, close or delete trees.
Security paths force approval at land even without a label. Filers label obvious
trust-boundary work; leads inspect the diff and add security-review when needed.
Only the operator or authorized policy owner removes that label.

_Version: 2.1.0_

_Last verified: 2026-11-03_
