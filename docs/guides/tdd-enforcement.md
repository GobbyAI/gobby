# Tests-first Development

Gobby expresses tests-first requirements through task metadata, developer skills,
and independent review. Developers run the tests; reviewers judge the evidence.

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

All developer agents load `gobby:references/development/obligations.md` before
implementation. That reference requires test judgment on every developer task and
tells the agent to load
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
  --min-severity low
```

Every reported finding must be fixed regardless of severity; raising
`--min-severity` to pass is not allowed. A missing baseline is not a skip
reason; the CLI treats current supported-language issues as new. Outside Gobby,
an unsupported-language warning must be paired with focused repo-native
validation.

## Review Gates

Independent reviewers inspect the tests-first commit sequence, the expected
failing behavior, final passing results, and test quality. Missing evidence is a
review finding for the developer to address. The close tool does not validate
RED/GREEN receipts or start an automatic review.

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

## Runtime Guidance

Task metadata selects the `test-driven-development` skill. Production edits are
not gated by test-write flags or daemon-issued TDD proof. The skill describes
how to reach a meaningful failing assertion, implement the behavior, and rerun
focused checks. Commit history and independently repeatable tests provide the
review evidence.

## Verification Checklist

When auditing this guide, verify:

- `compile_plan_to_spec` emits one leaf per manifest entry.
- TDD leaves include `additional_skills: ["test-driven-development"]`.
- TDD leaves include label `tdd:required`.
- TDD validation criteria require red, green, refactor/final-green, exact
  command, and supported test-quality audit or unsupported-language fallback
  evidence.
- Developer agents load `gobby:references/development/obligations.md`.
- Independent review checks the sequence and test quality.
- Close exposes only its four task/commit/attribution/ancestry invariants.

## See Also

- [Task Expansion](./task-expansion.md)
- [Test Quality](./test-quality.md)
- [Rules](./rules.md)
- [Variables](./variables.md)
- [Orchestration](./orchestration.md)

_Last verified: 2026-10-22_
