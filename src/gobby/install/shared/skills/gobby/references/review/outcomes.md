# Review outcomes

Load before recording an epic verdict, interpreting a feedback submission, or
learning from confirmed review findings. Discover current task/stage state first;
load [evidence](evidence.md) and the applicable memory review-learning reference.

## Epic verdict

Emit exactly one structured block:

```text
## Epic Findings
verdict: approve | request_changes | needs_discussion
spec_compliance: OK | Drift | Gap - <citation and rationale>
code_quality: OK | Drift | Gap - <citation and rationale>
testing: OK | Drift | Gap - <citation and rationale>
proportionality: OK | Drift | Gap - <citation and rationale>
findings:
- [blocking] <plan/child/file/commit citation>: <actionable issue>
- [nit] <citation>: <nonblocking observation>
```

Use OK only for passing checks, Gap for missing behavior/evidence and Drift for
extra or divergent behavior. Explain outcomes to the user as approve / reject /
escalate. Do not use `needs_discussion` for unattended work; judge available evidence
as approve or request changes.

For an open epic whose `epic_qa` stage is in progress, the reviewer maps:

- Approve → `gobby-tasks-ops:complete_stage` with `stage_name="epic_qa"` and
  `validation_override_reason="epic_qa approved by epic-reviewer"`, backed by the
  actual verdict evidence. This is the epic review policy's completion path.
- Request changes → `gobby-tasks-ops:fail_stage` with the verdict in `reason` and
  `cited_subtasks` naming every blocking descendant. Include at least one cited
  descendant; do not use unrelated tasks or an invented dispatch failure reason.
- Needs discussion → `gobby-tasks:escalate_task` with a `needs_human:` reason naming
  the concrete decision.

Do not substitute `approve_review`/`reject_review` for an in-progress epic QA
verdict: those operate on `needs_review` stages. Generic submission/approval flows
remain in [task reviews](../tasks/reviews.md). On an illegal transition, reread
state and ownership; do not bypass policy. Completion/failure can release the
prior claim and dispatch mutex. Follow returned stage state and retry/escalation
caps rather than assuming every failure immediately reruns review.

Closed-epic review records findings without stage transitions. A delegated
reviewer owns its verdict and terminates through its agent workflow; the launcher
does not repeat it. Review never closes the epic itself.

## Confirmed lessons

Recall `gobby-review-learning:recall_review_context` before finalizing rejection
triage. Preserve reusable findings through rejection and re-review; mint epic
lessons only after the fix is confirmed. Consult `list_check_keys` first. Each
lesson requires an explicit check key, principle/root cause, prevention, both
leaf task and path anchors, confirmed fix evidence and stable fingerprint.

Prove `qa-miss` (leaf QA approved while the defect remained) and `validation-miss`
(leaf validation passed while it remained) independently. Record one occurrence
per proven class with `source_kind=qa_rejection`, `source="epic-reviewer"`,
`decision=confirmed`, stable `source_review="epic-qa:<epic-ref>:<re-review-id>"`,
`pattern_id=epic-qa:<lesson_type>:<check-key>` and class-suffixed finding fingerprint.
`record_review_lesson` derives occurrence identity from `source_review` and
`finding_fingerprint`; set `guardrail_target=checklist` for qa-miss and
`validation` for validation-miss. Include the cited file in finding and evidence,
leaf task ref and confirmed fix proof; normalized lessons carry code-domain and
path tags. Incomplete or unproven classes mint nothing. Follow
[memory review learning](../memory/review-lessons.md) for the tool contract.

## Feedback acceptance versus task outcomes

Read `gobby-feedback:get_review_results(run_id="<run-id>", offset=0, limit=50)`;
follow `next_offset` to null. Results include clusters, status/error, review attempts
and page-related `filed`, `suppressed`, `deferred`, `failed`, `retained` outcomes.
Submission success persists findings/report; deterministic intake subsequently
records task outcomes. It does not prove all proposed tasks were filed or fixed.

The service files reviewed findings under its exact-title findings epic with
`feedback-review`, `llm-reviewed`, `awaiting-human-review`; guidance gaps also carry
`needs-decision`. Do not remove the human-review boundary as a side effect of
reading a report. Reports use one local-start-date daily path; dry-run reports
have a separate directory.

Failed actions leave their observations unreviewed; partial runs identify those
failures. Failed review, interrupted work, report-write failure and dry runs
preserve retryability. Inspect durable run evidence before relaunching; a valid
submission can survive reviewer failure/timeout. CLI digest and HTTP run readers
are operator diagnostics, documented in the
[HTTP guide](../../../../../../../../docs/guides/http-endpoints.md#feedback-review).
