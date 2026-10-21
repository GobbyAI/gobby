# Review outcomes

Load before recording an epic verdict or interpreting a feedback submission.
Read the epic's task state first; load [evidence](evidence.md).

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

Open and closed epics follow one path. The reviewer delivers the block in its
`end_agent_run` handoff and maps the verdict:

- Approve → the block is the whole verdict.
- Request changes → create a remediation task under the epic for every blocking
  finding, citing the blocking descendant. On a closed epic whose delivered work
  must be reworked, `gobby-tasks:reopen_task` replaces the remediation tasks.
- Needs discussion → `gobby-tasks:escalate_task` with a `needs_human:` reason naming
  the concrete decision.

A delegated reviewer owns its verdict and terminates through its agent workflow;
the launcher does not repeat it. Review never closes the epic itself, and the
`/gobby review` caller acts on the verdict.

## Feedback acceptance versus task outcomes

Read `gobby-feedback:get_review_results(run_id="<run-id>", offset=0, limit=50)`;
follow `next_offset` to null. Results include clusters, status/error, review attempts
and page-related `filed`, `suppressed`, `deferred`, `failed`, `retained` outcomes.
Submission success persists findings/report; deterministic intake subsequently
records task outcomes. It does not prove all proposed tasks were filed or fixed.

The service files reviewed findings under its exact-title findings epic with
`feedback-review`, `llm-reviewed`, `awaiting-human-review`; intake adds no hold
label such as `needs-decision`. Do not remove the human-review boundary as a side
effect of reading a report. Reports use one local-start-date daily path; dry-run reports
have a separate directory.

Failed actions leave their observations unreviewed; partial runs identify those
failures. Failed review, interrupted work, report-write failure and dry runs
preserve retryability. Inspect durable run evidence before relaunching; a valid
submission can survive reviewer failure/timeout. CLI digest and HTTP run readers
are operator diagnostics, documented in the
[HTTP guide](../../../../../../../../docs/guides/http-endpoints.md#feedback-review).
