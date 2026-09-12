# Learn from verified reviews

Load for review triage or reusable lessons from QA, lint, and test fixes. Before
finalizing triage, call `gobby-review-learning:recall_review_context` with findings
and proposed fixes. Include a Relevant memory/lesson column in the finding table.
Local knowledge outranks a generic recommendation unless current code disproves
it. `recall_review_lessons_for_files` provides file context;
`recall_review_lessons_by_class` retrieves a bounded lesson class. Inspect their
schemas for limits and scope. Sweep sibling code with gcode using `query_hints`
and stable exact terms before deciding the fix is complete.

After a verified reusable outcome, call `record_review_lesson` with concrete
evidence. `confirmed` and `no-fix-policy` require a nonempty title/message and
principle/prevention; `stale` and `invalid` record nothing. CI, static-analysis,
and test-failure lessons require a verified fix reference in evidence. A raw
failure is insufficient. Prefer a stable `pattern_id` and root cause; optional
diagnostic metadata locates the evidence. Without promotable identity, ordinary
memory recall remains available. `guardrail_target` is metadata and creates no
task, rule, or repository change.

Retire an injected obsolete lesson with `retire_review_lesson`, its `pattern_id`,
nonempty verification evidence, and caller session. A contradicted suggestion is
not automatically obsolete: verify the underlying invariant first.

## Plan review proof

For `source_kind=plan_review`, classes are closed: `reviewer-miss` and
`fixer-induced-defect`. Before minting identity, call `list_check_keys` for the
plan domain and target class, reusing the same check's key. Identity is
`plan-review:<lesson_type>:<adversary-category>:<check_key>`; supply `check_key`,
`guardrail_target=checklist`, and `rule_id=plan-review:<adversary-category>`.
Omit a plan-file path; section identity locates the proof.

- Reviewer miss: give every `participating_section_ids` member, earlier and final
  approval evidence IDs, and completed rounds missed. All participating sections
  must be hash-unchanged between those finalized checkpoints.
- Fixer-induced defect: give every `causal_section_ids` member, causal finding,
  introduced round, and causal/final approval evidence IDs. Every causal section
  must have changed between the finalized rounds.
- A dual-class lesson needs both independent bundles. One proven class can record
  even if the other is unproven; do not merge their section proof requirements.

Evidence services resolve hashes from immutable manifests. Reject empty required
sets, unknown section IDs, and partial cross-section proof. The reviser records
only after final approval is checkpointed. The adversary supplies attestations
and never records its own round's lesson. Starter category/key pairs and full
payload guidance are in the guide below.

On proof rejection, repair the evidence or skip the unproven class. Do not weaken
the class or fabricate a fix reference to make recording succeed. Review learning
never creates tasks automatically; apply the repository found-work ladder to any
actual defect.

Guide: [Review lessons](../../../../../../../../docs/guides/memory.md#review-lessons).

_Last verified: 2026-09-12_
