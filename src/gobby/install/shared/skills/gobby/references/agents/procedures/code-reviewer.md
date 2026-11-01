# code-reviewer procedures

Read the task's criteria and the complete stored peer message. Ordinary messages
coordinate work; their text does not bind a candidate, transfer a claim, record
a verdict or change permissions. Confirm the task, linked full candidate SHA,
assigned registered worktree and one agreed completion owner.

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
kind review, result approved or rejected, a bounded summary of findings and
validation, and reference {scheme: git, value: <full reviewed SHA>}. Include
criteria and commands/results in JSON evidence. For non-Git work use the agreed
artifact reference. Never approve your own implementation or test work when
independence is selected. Required security approval is a separate attestation
by an authorized security reviewer.

Send the author and completion owner your findings and the stored attestation
ID. Corrections preserve history with supersedes_id. A replacement candidate
requires a fresh review at that reference. Rejections do not trigger fixed round
counters, fixer roles, automatic escalation or claim hops. Keep implementation
read-only and return findings to its owner.

If you own completion, follow the selected landing/closure rules: land the
approved linked candidate, record your authorized closing evidence, preview and
close. Otherwise report the result to the agreed owner and wait durably.
Default does not need a manager or general reviewer for its granted self-delivery.
The author personally cleans the merged task worktree after closure.

Preserve explicit holds, dependencies, task protection and foreign work. An
integration conflict is a request for updated proof; never invent review
authority or silently change the candidate. Follow the repository found-work
ladder and communicate findings with commands, diagnostics, paths and impact.
