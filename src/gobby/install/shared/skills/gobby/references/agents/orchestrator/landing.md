# Orchestrator: landing and activation

Load before landing, ruling on security approval, recording evidence, directing
a restart or cutover, pushing, or closing your own live fix.

## Landing

- Security approval. Add `security-review` when the task requires it. The selected security policy requires the latest approved `security-review` attestation for the exact Git candidate from an independent author whose installed definition declares `approve_security_review`. A general review or a message is insufficient; a later rejection revokes approval.
- Landing. A seat whose installed definition grants the capability, task scope and path coverage may use `gobby-tasks-ops:land_commit` when the selected policy passes. Inspect the exact refusal and its blockers; preserve ownership, conflicts, freezes and required security approval. Confirm the integration commit before closing.
- Evidence. Use `record_attestation` for any kind, result, summary, structured evidence and optional artifact references. Each author writes its own evidence; never invent another author's approval or record a check that did not occur. Preview closure to see unmet requirements.
- Never gate a local landing, package or daemon restart on a review of pushed work. The responsible author resolves each post-push finding with a fix or a reasoned dismissal.
- After every landing, confirm the merge.

## Activation

- The daemon announces its own restarts and cutovers; never compose those notices by hand. A landing meant to fix load activates at once, even under heavy load.
- Landing ledger. You decide every restart and activation outside the project policy's quiet hours; the Release Manager runs them on your direction. Launch it with `support` and `seats=release-manager` when it isn't live, and run one yourself only when it can't. No other seat runs `gobby restart`. Keep the ledger of landed work.
- Restarts. Activate each landing that needs it (rules, definitions, daemon code) with a restart outside the project policy's quiet hours as soon as it lands; don't hold it back to batch with later landings. Do not spawn while a restart or cutover is in progress. After the restart, read back each installed row the landing changed, then close.
- Pushes. Prepare a SHA that passes the pre-push checks; the Assistant brings it to the operator. Once the operator authorizes that exact SHA, run `git push origin <sha>:refs/heads/<branch>`.

## Land and close your own live fix

In order (details in `gobby:references/tasks/closing.md`):

1. `gobby-tasks-ops:land_commit`; direct the restart or cutover if the landing needs activation.
2. Record the closing attestation with the full integrated SHA.
3. `release_task_claim` if a claim is still held.
4. `close_task` with `preview=true`, then the real call with the returned `expected_closure_revision`.
5. `stop_agent` on each remaining seat of the task, with the full run UUID.
6. `delete_worktree` with `merged_into` set to the tree's stored base branch.
7. `review_task_memories` for the closed task.

## Security boundary policy

The static Support security-auditor drafts security_paths from a brownfield audit
or greenfield boundary design and proposes additions found in reviews.
The Assistant brings Approve/Hold to the operator; on approval, apply the draft to
the installed project-policy source, whose rules refresh the projection.
The operator never hand-edits it. Only the operator or
the Orchestrator's declared remove_security_review_label authority removes the label.
Filers label obvious trust-boundary tasks; leads inspect every candidate diff.

_Version: 1.0.0_

_Last verified: 2026-11-05_
