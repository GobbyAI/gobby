---
name: epic-review
description: Review an implemented epic against its approved plan, aggregate diff, and child-task validation evidence before final PR or merge handoff.
version: "1.0.0"
category: methodology
internal: true
triggers: epic review, epic review, implementation audit, intent vs diff
metadata:
  gobby:
    audience: all
    depth: 0
---

# epic-review - Gobby Epic Implementation Review

> Internal methodology skill; loaded with `get_skill(name="epic-review")`
> by the `epic-reviewer` agent before it reviews an implemented epic.

REQUIRED SKILL: review-learning.
REQUIRED SKILL: proportionality.

Use this skill to decide whether an epic's implementation matches the approved
plan or equivalent review scope, the observed diff, and the linked subtasks'
validation criteria.

## Inputs

Review all available evidence before deciding:

- The epic task and its current lifecycle labels.
- The approved plan artifact at `epic.plan_file_path`, when one exists.
- For docs/build epics without a plan artifact, the task's Discovery Brief,
  validation criteria, and full descendant task set are an acceptable plan
  substitute.
- The aggregate worktree diff for the epic.
- Linked subtask descriptions, validation criteria, commits, and close notes.
- The coverage matrix or plan-to-task mapping when available.

If the plan artifact is unavailable, do not escalate solely for that reason
when the epic has equivalent evidence such as a Discovery Brief plus descendant
tasks. If neither a plan artifact nor an equivalent review scope exists, escalate
with a `needs_human:` reason. If the aggregate diff is unavailable, reconstruct
the relevant diff from descendant commits or the integration branch when
possible; escalate only when no reliable implementation evidence is available.
Never use `needs_discussion` under yolo mode; yolo mode still requires a
concrete approve or request-changes outcome from available evidence.

## Methodology

Walk the evidence mechanically in this order.

### spec_compliance

Compare the approved plan or plan substitute, coverage matrix, full task
subtree, aggregate diff, and delivered behavior. Catch missing plan items,
child-work gaps, integration failures, scope drift, omitted cleanup, and
divergent product behavior here. Reject on missing or incorrect required
behavior before moving to code quality.

### code_quality

After spec_compliance passes, review cross-cutting maintainability,
architecture fit, consistency across leaves, duplicate or fragile
implementations, safety, and repo-pattern alignment.

### testing

Evaluate validation evidence against epic-level risk and end-to-end behavior.
Coverage can be unit, integration, E2E, regression, contract, manual, or
infrastructure depending on what the plan required.

For descendant tasks marked `tdd:required`, requesting
`test-driven-development`, or carrying validation criteria that require TDD,
verify that QA and completion evidence include the expected red failure,
minimal green pass, refactor/final-green pass, exact test command, and
test-quality audit output for supported touched tests. A missing baseline is
not a skip reason. Outside Gobby, an unsupported-language warning is acceptable
only with focused repo-native validation evidence. Missing TDD evidence is a
testing gap and blocks approval.

### proportionality

Apply the shared `proportionality` criterion (anti-Rube-Goldberg) at epic
altitude: flag mechanism with no concrete consumer or requirement in the plan —
speculative abstractions, unnecessary rewrites, and indirection the plan never
needed. Weigh the cross-leaf signals only an epic-level view can see: duplicate
frameworks built independently across leaves, a framework introduced by one leaf
and used by none, and product behavior no plan section asked for. Size,
ambition, and a large but justified epic are never findings on their own; name
the simpler form whenever you flag.

## Finding Attribution

Every blocking finding must cite the plan section or plan-substitute item it
violates. Use `### N.N` plan sections as the stable attribution unit when they
exist, because expansion-generated subtasks are mapped from those sections. For
docs/build epics that use a Discovery Brief plus descendant task set, cite the
specific child task reference, validation criterion, Discovery Brief bullet,
file path, or commit that demonstrates the gap.

If a finding cannot be attributed to a `### N.N` section, explain whether the
plan omitted the requirement, the substitute scope omitted it, or the
implementation drifted beyond the plan.

## Epic Findings

Return a structured verdict block in this shape:

```text
## Epic Findings

verdict: approve | request_changes | needs_discussion
spec_compliance: OK | Drift | Gap - <citation and one-line rationale>
code_quality: OK | Drift | Gap - <citation and one-line rationale>
testing: OK | Drift | Gap - <citation and one-line rationale>
proportionality: OK | Drift | Gap - <citation and one-line rationale>

findings:
- [blocking] <plan section, subtask, file/commit citation>: <actionable issue>
- [nit] <citation>: <non-blocking observation>
```

Use `OK` only when the check passes. Use `Gap` for missing required behavior or
evidence. Use `Drift` for extra or divergent implementation.

Before returning `request_changes`, call
`gobby-review-learning.recall_review_context` for the blocking findings and
include relevant local memory/lesson context in the verdict. For reusable
rejection patterns, call `gobby-review-learning.record_review_lesson` before the
verdict with `source_kind=qa_rejection`; do not record one-off, stale, or invalid
findings.

## Epic Lesson Recording

Keep every reusable `## Epic Findings` entry through rejection and re-review
with this finding/confirmation schema:

- `check_key`: an explicit kebab-case key. Call `list_check_keys` and consult the
  existing catalog before selecting it; the recorder performs final validation.
- `lesson_classes`: one or both of `qa-miss` and `validation-miss`, supported by
  the cited leaf evidence.
- At least one of `principle` or `root_cause`, plus a non-empty `prevention`.
- A concrete anchor containing both `leaf_task_ref` and `path`.
- `confirmed_fix_evidence`: the commit or changed files and focused passing
  validation that prove the fix on re-review.
- `finding_fingerprint`: a stable identity for the epic finding.

Incomplete entries mint nothing. This includes a missing or invalid
`check_key`, an empty `lesson_classes` value, neither `principle` nor
`root_cause`, missing `prevention`, either missing anchor component, missing
`confirmed_fix_evidence`, or missing `finding_fingerprint`. Keep an incomplete
entry in the verdict for remediation; never send it to
`record_review_lesson`.

Mint only when the fix is confirmed on re-review. Record each proven class
independently:

- `qa-miss`: leaf QA approved the cited leaf while the epic finding remained.
  Use `guardrail_target=checklist`.
- `validation-miss`: leaf validation passed while the epic finding remained.
  Use `guardrail_target=validation`.

For both classes, use `source_kind=qa_rejection`, `source="epic-reviewer"`,
`decision=confirmed`, and a stable
`source_review="epic-qa:<epic-ref>:<re-review-id>"`. Use the class-scoped
identity `pattern_id=epic-qa:<lesson_type>:<check-key>` and
`finding_fingerprint=<stable-finding-id>:<lesson_type>`, then derive the
occurrence identity with
`build_occurrence_key(source_review, finding_fingerprint)`. Record the cited
file as both `finding.path` and in `evidence.files`, with
`leaf_task_ref` and `confirmed_fix_evidence` in evidence. The normalized lesson
must carry `lesson-domain:code` and path tags from the cited files.

One finding that proves both classes produces two lesson records with separate
pattern keys, occurrence keys, and occurrence counts. A class without complete
proof records nothing while another proven class remains independently
recordable.

## Decision Mapping

Map the verdict to task lifecycle tools exactly:

- `approve` means call `complete_stage(stage_name="epic_qa")` on the epic
  with a validation override reason such as
  `epic_qa approved by epic-reviewer`.
- `request_changes` means call `fail_stage(stage_name="epic_qa")` with the
  verdict in the reason and `cited_subtasks` for every blocking finding. At
  least one cited subtask is required.
- `needs_discussion` means call `escalate_task` with a reason that starts with
  `needs_human:` and names the concrete decision needed.

Do not close tasks from epic review. The dispatcher or merge/PR flow handles
the next lifecycle move after the review decision is recorded.
