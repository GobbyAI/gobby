---
name: holistic-review
description: Review an implemented epic against its approved plan, aggregate diff, and child-task validation evidence before final PR or merge handoff.
version: "1.0.0"
category: core
internal: true
triggers: holistic review, epic review, implementation audit, intent vs diff
metadata:
  gobby:
    audience: all
    depth: 0
---

# holistic-review - Gobby Epic Implementation Review

> Internal methodology skill; loaded with `get_skill(name="holistic-review")`
> by the `holistic-reviewer` agent before it reviews an implemented epic.

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
test-quality audit output for touched tests. Missing TDD evidence is a testing
gap and blocks approval.

### yagni

Flag speculative abstractions, unnecessary rewrites, duplicate frameworks,
scope creep, and unplanned product behavior that was not needed to deliver the
plan.

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

## Holistic Findings

Return a structured verdict block in this shape:

```text
## Holistic Findings

verdict: approve | request_changes | needs_discussion
spec_compliance: OK | Drift | Gap - <citation and one-line rationale>
code_quality: OK | Drift | Gap - <citation and one-line rationale>
testing: OK | Drift | Gap - <citation and one-line rationale>
yagni: OK | Drift | Gap - <citation and one-line rationale>

findings:
- [blocking] <plan section, subtask, file/commit citation>: <actionable issue>
- [nit] <citation>: <non-blocking observation>
```

Use `OK` only when the check passes. Use `Gap` for missing required behavior or
evidence. Use `Drift` for extra or divergent implementation.

## Decision Mapping

Map the verdict to task lifecycle tools exactly:

- `approve` means call `complete_stage(stage_name="holistic_qa")` on the epic
  with a validation override reason such as
  `holistic_qa approved by holistic-reviewer`.
- `request_changes` means call `fail_stage(stage_name="holistic_qa")` with the
  verdict in the reason and `cited_subtasks` for every blocking finding. At
  least one cited subtask is required.
- `needs_discussion` means call `escalate_task` with a reason that starts with
  `needs_human:` and names the concrete decision needed.

Do not close tasks from holistic review. The dispatcher or merge/PR flow handles
the next lifecycle move after the review decision is recorded.
