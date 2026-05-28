# TDD Enforcement

Gobby enforces TDD through expansion metadata, native developer skills, review
criteria, and lightweight runtime nudges.

## Expansion Shape

New expansion specs emit one leaf per manifest entry. There are no generated
test/implementation/refactor wrapper tasks.

For a manifest entry with `tdd: true`, the compiler adds:

- `additional_skills: ["test-driven-development"]`
- label `tdd:required`
- validation criteria requiring red evidence, minimal green evidence,
  refactor/final-green evidence, the exact test command, and test-quality audit
  output for supported touched test paths or unsupported-language fallback
  evidence outside Gobby

Only `code` and eligible `config` entries may set `tdd: true`. Use it for
`config` only when the plan identifies executable behavior that can be pinned
before the config change.

## Developer Skills

All developer agents load `development-discipline` before implementation. That
skill requires test judgment on every developer task and tells the agent to load
`test-driven-development` when the task is marked `tdd:required`, requests the
additional skill, or has validation criteria requiring TDD.

The `test-driven-development` skill requires:

1. Add or update the smallest meaningful test before implementation.
2. Run the exact focused command and verify the expected red failure.
3. Implement the smallest change that makes the test pass.
4. Run the focused command and verify green.
5. Refactor only after green, then run final green validation.
6. Run `gobby test-quality audit` on supported touched test paths after adding
   or heavily editing tests.

For noisy test areas, use baseline mode:

```bash
uv run gobby test-quality audit <paths> \
  --baseline .gobby/test-quality-baseline.json \
  --fail-on-new \
  --min-severity high
```

A missing baseline is not a skip reason; the CLI treats current
supported-language issues at or above `--min-severity` as new. Outside Gobby,
an unsupported-language warning must be paired with focused repo-native
validation.

## Review Gates

`qa-reviewer` checks TDD-required leaves before approval. Missing red evidence,
green evidence, refactor/final-green evidence, exact test command, or
supported-language test-quality audit output is a rejection. Outside Gobby, an
unsupported-language warning plus focused repo-native validation satisfies the
audit-attempt evidence.

`holistic-reviewer` checks the aggregate subtree. If a descendant task was
TDD-required, holistic QA verifies that QA and completion evidence covered the
same TDD and test-quality requirements.

## Planning Rules

Plan authors describe behavior and acceptance criteria. They do not add filler
tasks such as:

- `Write tests for ...`
- `Add tests for ...`
- `Ensure tests pass`
- `[TDD] ...`
- `[IMPL] ...`
- `[REF] ...`

Standalone `category: test` remains valid for test infrastructure,
characterization, parity, or regression suites with their own acceptance
criteria.

## Runtime Nudges

Workflow rules still provide test-first nudges for interactive developer
sessions:

| Rule | Event | Path |
|------|-------|------|
| `enforce-tdd-block` | `before_tool` | `src/gobby/install/shared/workflows/rules/tdd-enforcement/enforce-tdd-block.yaml` |
| `enforce-tdd-track-tests` | `after_tool` | `src/gobby/install/shared/workflows/rules/tdd-enforcement/enforce-tdd-track-tests.yaml` |

These rules inspect write/edit tool calls when `enforce_tdd` is true. They are
supporting guardrails; the authoritative requirement for new expansion leaves
is the task metadata and completion evidence.

## Verification Checklist

When auditing this guide, verify:

- `compile_plan_to_spec` emits one leaf per manifest entry.
- TDD leaves include `additional_skills: ["test-driven-development"]`.
- TDD leaves include label `tdd:required`.
- TDD validation criteria require red, green, refactor/final-green, exact
  command, and supported test-quality audit or unsupported-language fallback
  evidence.
- Developer agents load `development-discipline`.
- QA and holistic QA agent definitions mention TDD evidence and test-quality
  audit requirements.
- Runtime rule paths still live under
  `src/gobby/install/shared/workflows/rules/tdd-enforcement/`.

## See Also

- [Task Expansion](./task-expansion.md)
- [Test Quality](./test-quality.md)
- [Rules](./rules.md)
- [Variables](./variables.md)
- [Orchestration](./orchestration.md)

_Last verified: 2026-05-28_
