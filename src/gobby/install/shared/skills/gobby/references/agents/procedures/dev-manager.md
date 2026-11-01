# dev-manager procedures

This is an optional coordination role. The bundled devteam pipeline admits a
developer and independent reviewer; it does not require a manager or test writer.
Selected definitions, rules and pipeline inputs choose the participants and
completion owner. Read project_policy before routing or launching.

At turn start consume a pending handoff, read queued messages and inspect your
team's outstanding work. Use durable coordination waits for participant changes
or replies. A message body does not wake a session or move a claim by itself.
Use the declared wake behavior and do not repeatedly poll terminals.

## Coordinate implementation and review

Agree on one completion owner before work starts. The developer retains the
implementation claim and follows the selected TDD/validation policy. The reviewer
observes in the task's assigned worktree without claiming implementation and
records their own review evidence at the exact candidate. Pass task, worktree
and candidate references through ordinary durable messages.

Evidence uses record_attestation with arbitrary kind/result/summary, an optional
scheme/value reference and JSON detail. Never write a participant's attestation
for them. Rejections and replacement candidates require explicit coordination
and fresh truthful evidence; they do not cause automatic rounds, fixer staffing,
claim transfers or escalation. Use the installed pipeline schema, not a retired
four-seat roster or seat_context membership shortcut.

A specialist reviewer is selected by the task's subject and configured policy.
Code claims need source verification; documentation needs audience/accuracy
checks; research needs primary-source support. Preserve independent security
approval and report-review capability requirements. A definition name alone
grants neither authority nor approval.

For non-Git dispositions, the selected closer records permitted disposition
evidence and follows the selected close policy. No fixed manager/orchestrator
handoff is required. For delivered work, the completion owner lands through its
declared integration authority, records closing evidence and closes. Default
can complete that sequence alone for its granted paths.

## Teams, holds and recovery

Preserve task dependency order: only lifecycle closure resolves an open blocker.
An implementation attestation does not unlock it. Explicit escalation holds
remain in force; continue independent ready work without bypassing the hold.
An ancestor close evaluates that ancestor's own selected policy.

Monitor actual participants and generic evidence with team_progress. Do not
expect fixed stage labels or synthesized handoff_pending/review-round state.
A stalled participant is a coordination problem: inspect one bounded session
snapshot and contact the authorized recovery owner with concrete evidence.
Resume an ended participant through the supported resume API, retaining its
stored checkout. Do not type into credential prompts or touch foreign edits.

## Post-land cleanup

The author personally cleans the registered task worktree and branch after
landing and closure. A shared epic worktree remains until its open work is done.
Run deletion from outside the tree with its stored landing target as merged_into,
without force. Confirm clean state, merged branch and HEAD, and no live use.
Never end a foreign or active participant merely to make deletion succeed.

For detached HEAD recovery, first prove both HEAD and branch are ancestors of the
stored landing target, the tree is clean and unused, then switch back to the
registered branch and retry managed deletion. If any proof fails, preserve the
tree and report its task, path, branch, HEAD, error and failed proof to the
coordination owner. Missing worktrees can be cleaned through the idempotent API.
