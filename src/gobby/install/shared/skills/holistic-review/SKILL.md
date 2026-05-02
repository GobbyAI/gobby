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
plan, the observed diff, and the linked subtasks' validation criteria.

## Inputs

Review all available evidence before deciding:

- The epic task and its current lifecycle labels.
- The approved plan artifact at `epic.plan_file_path`.
- The aggregate worktree diff for the epic.
- Linked subtask descriptions, validation criteria, commits, and close notes.

If the plan artifact or diff is unavailable, escalate with a `needs_human:`
reason unless the prompt explicitly provides equivalent evidence. Never use
`needs_discussion` under yolo mode; yolo mode still requires a concrete approve
or request-changes outcome from available evidence.

## Methodology

Walk the evidence mechanically with these four checks.

### Scope

Compare the approved plan to the final diff. The implementation should do
exactly what the plan called for: no missing plan sections, no unrelated product
behavior, no unplanned rewrites, and no omitted cleanup or migration steps.

### Reality

Verify the end-to-end behavior implied by the plan is actually present in the
changed code and task outputs. Do not accept a change just because individual
subtasks were closed if their combined behavior does not produce the planned
outcome.

### Testing

Check whether tests, fixtures, validation commands, or manual verification cover
the risk introduced by the diff. Coverage can be unit, integration, E2E,
regression, contract, or infrastructure depending on what the plan required.

### YAGNI

Look for drift, creep, or scope bloat. Flag extra abstractions, speculative
framework work, broad rewrites, or added product behavior that was not needed to
deliver the plan.

## Finding Attribution

Every blocking finding must cite the plan section it violates. Use `### N.N`
plan sections as the stable attribution unit, because expansion-generated
subtasks are mapped from those sections. When possible, also cite the linked
subtask, file path, or commit that demonstrates the gap.

If a finding cannot be attributed to a `### N.N` section, explain whether the
plan omitted the requirement or the implementation drifted beyond the plan.

## Holistic Findings

Return a structured verdict block in this shape:

```text
## Holistic Findings

verdict: approve | request_changes | needs_discussion
scope: OK | Drift | Gap - <citation and one-line rationale>
reality: OK | Drift | Gap - <citation and one-line rationale>
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

- `approve` means call `approve_review(stage_name="holistic_qa")` on the epic with the verdict
  block as approval notes.
- `request_changes` means call `reject_review(stage_name="holistic_qa")` with
  `rejection_notes` and `cited_subtasks`. At least one cited subtask is required.
- `needs_discussion` means call `escalate_task` with a reason that starts with
  `needs_human:` and names the concrete decision needed.

Do not close tasks from holistic review. The dispatcher or merge/PR flow handles
the next lifecycle move after the review decision is recorded.
