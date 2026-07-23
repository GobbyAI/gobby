---
name: development-discipline
description: "Developer-agent discipline for test judgment, validation evidence, and TDD-required task escalation."
version: "1.0.0"
category: core
internal: true
triggers: development discipline, developer validation, test judgment
metadata:
  gobby:
    audience: all
---

# Development Discipline

Use this for every developer-agent task.

## Required Judgment

Before editing, make an explicit test judgment: decide how behavior can be
verified. Name the relevant test level: unit, integration, CLI/API, browser/UI,
migration/storage, or documented manual check.

Every completion or review handoff must include:

- Exact validation commands run.
- Result of each command.
- Why those commands cover the changed behavior.
- Any test gap that remains, with the reason it is acceptable.

## Validation Scope

Do not run full test suites as a spawned agent. Use focused files, packages,
or test-name filters for the changed behavior.

For Rust work, do not run bare `cargo test` or workspace-wide
`cargo test --no-default-features`. Use focused commands such as
`cargo test -p <package>` or `cargo test <name> -p <package>`.

## Test Changes

Add or update tests when behavior changes. Skipping tests is acceptable only for
pure documentation, mechanical metadata, or code paths that cannot be executed
locally; document the reason in the task handoff.

When adding or heavily editing tests, run `gobby test-quality audit` on touched
test paths when the touched test language is supported. For noisy areas, use:

```bash
uv run gobby test-quality audit <paths> --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity high
```

Do not skip the audit because `.gobby/test-quality-baseline.json` is missing;
the CLI treats current supported-language issues at or above `--min-severity`
as new. If the audit reports an unsupported-language warning outside the Gobby
repo, include that warning plus focused repo-native validation evidence.

When adding or heavily editing Python tests, also run `gobby test-types audit`
on the touched test paths:

```bash
uv run gobby test-types audit <paths> --baseline .gobby/test-types-baseline.json --fail-on-new
```

Fix new test type errors directly. Use `typing.cast` at intentional invalid-input
boundaries; never add `# type: ignore` comments (#14544). After reducing existing
debt, safely regenerate the baseline with:

```bash
uv run gobby test-types audit <paths> --baseline .gobby/test-types-baseline.json --fail-on-new --write-baseline .gobby/test-types-baseline.json
```

This writes only after the ratchet passes. Reserve `--allow-failing-baseline`
for explicitly reviewed additions.

## TDD-Required Tasks

If the task has label `tdd:required`, `additional_skills` contains
`test-driven-development`, or validation criteria require TDD evidence, load
`test-driven-development` before implementation and follow it exactly.

## Recurring Validation Lessons

After a successful task close returns `recurring_validation_candidates`, record
exactly one lesson per task with `record_review_lesson`. The candidates are the
prompt to record; the passing close is the proof that the lesson is confirmed.

Apply this contract:

1. Order candidate groups by recurrence count descending, then group title ascending.
   Select the first candidate only.
2. Consult `list_check_keys` for the candidate's `check_key`.
3. Set `source_kind=task_validation`, canonical `source="task-validation"`,
   task-scoped `source_review="task-validation:<task_uuid>"`, and
   `lesson_type=recurring-validation-failure`.
4. Set
   `pattern_id=task-validation:recurring-validation-failure:<check-key>` and
   `guardrail_target=validation`.
5. Build the finding from the candidate group. Include evidence that cites the
   failed validation iterations and the passing close.
6. Derive non-empty `prevention` and at least one non-empty `principle` or
   `root_cause` from that confirmed failure-and-pass evidence. Normalize the
   issue location into a supported implementation anchor on the finding's
   anchor fields: a file path or symbol, never free prose.

When the candidate cannot establish every required actionable promotion signal,
record nothing. Do not record a second candidate for the same task.

Occurrence identity is exactly
`build_occurrence_key(source_review, finding_fingerprint)`. The task-scoped
review ID makes byte-identical findings from separate tasks distinct
occurrences of one shared pattern, while rerecording within the same task
deduplicates the occurrence.
