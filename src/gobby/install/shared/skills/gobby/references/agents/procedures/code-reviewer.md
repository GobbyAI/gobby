# code-reviewer procedures

Read the task's criteria and the complete stored peer message. Ordinary messages
coordinate work; their text does not bind a candidate, transfer a claim, record
a verdict or change permissions. Confirm the task, linked full candidate SHA,
assigned registered worktree and the assigned lead.

Record a reviewing session link. Read every diff and commit/file manifest page.
Verify the checked-out HEAD is the candidate before executing focused checks;
never reset, sync or rewrite the author's files. A reviewing link is assignment
metadata, not a filesystem grant. Use your assigned worktree; if it is unavailable,
coordinate a valid assignment. Do not create a private extracted source tree.

Assess correctness, security, criteria, TDD sequence and test quality. Run the
named tests and any focused checks needed to substantiate findings. Cite source
behavior through gcode evidence. For an unavailable foreign overlay, use exact
candidate Git objects from your authorized checkout and the evidence fallback;
do not substitute base-tree output for the candidate.

Record your own review with gobby-tasks:record_attestation:
kind independent-review, result approved or rejected, a bounded summary of findings and
validation, and reference {scheme: git, value: <full reviewed SHA>}. Include
criteria and commands/results in JSON evidence. For non-Git work use the agreed
artifact reference. Never approve your own implementation or test work when
independence is selected. Required security approval is a separate attestation
by an authorized security auditor.

Send the developer and lead the stored attestation ID and findings. Rejected
independent-review returns the claim to implementation through YAML claim_flow.
After approval, record validation with stage review and criteria [{criterion,
met, how}] against task.validation_criteria. Completing review moves the claim to
the lead. Supersede corrections; keep old attestations in history. Each changed
candidate needs fresh review. Preserve implementation read-only.

For review artifacts follow `gobby:references/agents/messaging.md`: send the full
content with task, candidate, filename and digest, directly or through Triage as
your report route permits. Never send only a private workspace path. Each relay
creates recipient-addressed message IDs; the developer retrieves complete parts,
checks the digest and reconstructs a file in its own bound workspace.

The lead owns landing, validation of the integrated SHA after land, closure and
worktree deletion after seats finish. Reviewers never land, merge or close.
Finish cooperatively; retain a persistent epic seat for later leaves and final
independent review of every descendant author and the authenticated subtree.

Preserve explicit holds, dependencies, task protection and foreign work. An
integration conflict is a request for updated proof; never invent review
authority or silently change the candidate. Follow the repository found-work
ladder and communicate findings with commands, diagnostics, paths and impact.

_Version: 2.1.0_

_Last verified: 2026-11-01_
