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

## Structural Preflight

Before editing:

1. Identify the capability that owns the change.
2. Search with `gcode` for established placement and dependency patterns.
3. Check dependency direction, state ownership, public-surface impact, and test
   placement.

For package creation, module movement, cross-package dependencies, shared
abstractions, or ownership changes, REQUIRED SKILL: repository-maintenance.
For production file decomposition, REQUIRED SKILL: decompose-monolith.

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
test paths when the touched test language is supported:

```bash
uv run gobby test-quality audit <paths> --baseline .gobby/test-quality-baseline.json --fail-on-new --min-severity low
```

Fix every reported finding regardless of severity — low and medium findings are
defects, not noise. Never raise `--min-severity` to pass the gate. Do not skip
the audit because `.gobby/test-quality-baseline.json` is missing; the CLI treats
current supported-language issues as new. If the audit reports an
unsupported-language warning outside the Gobby repo, include that warning plus
focused repo-native validation evidence.

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
