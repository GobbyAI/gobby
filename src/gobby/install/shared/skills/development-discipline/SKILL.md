---
name: development-discipline
description: "Developer-agent discipline for test judgment, validation evidence, and TDD-required task escalation."
version: "1.0.0"
category: core
triggers: development discipline, developer validation, test judgment
metadata:
  gobby:
    audience: agent
    depth: 1
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

## TDD-Required Tasks

If the task has label `tdd:required`, `additional_skills` contains
`test-driven-development`, or validation criteria require TDD evidence, load
`test-driven-development` before implementation and follow it exactly.
