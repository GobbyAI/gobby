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

`qa-reviewer` checks TDD-required leaves before approval. Missing red evidence,
green evidence, refactor/final-green evidence, exact test command, or
supported-language test-quality audit output is a rejection. Outside Gobby, an
unsupported-language warning plus focused repo-native validation satisfies the
audit-attempt evidence.

`epic-reviewer` checks the aggregate subtree. If a descendant task was
TDD-required, epic QA verifies that QA and completion evidence covered the
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

Workflow rules provide an edit-time test-first gate for interactive developer
sessions:

| Rule | Event | Path |
|------|-------|------|
| `enforce-tdd-block` | `before_tool` | `src/gobby/install/shared/workflows/rules/tdd-enforcement/enforce-tdd-block.yaml` |
| `enforce-tdd-track-tests` | `after_tool` | `src/gobby/install/shared/workflows/rules/tdd-enforcement/enforce-tdd-track-tests.yaml` |

The gate activates when `enforce_tdd` is true or a claimed task requires TDD
through its label, additional skill, validation criteria, or session policy.
It covers every hand-maintained source extension used by the monolith guard:
Python, TypeScript/JavaScript, CSS, Rust, and shell. Test-convention paths in any
language are recognized by one shared classifier.

Claim refresh derives `claimed_task_requires_tdd` and the ordered,
deduplicated `claimed_task_acceptance_test_paths` from current task metadata.
When acceptance criteria name tests, writing any named path records its
canonical repository-relative path in `tdd_tests_written` and opens the source
write gate. Without named acceptance tests, any test-convention file under the
task's targets opens it. Retrying the same production write does not bypass the
gate, and the rule never rewrites task validation criteria.

These rules are supporting guardrails; task metadata and transcript-backed
completion evidence remain authoritative at close.

The rule files are bundled templates, not evidence of active enforcement.
Inspect installed rows with `gobby-workflows:get_rule` before diagnosing a live
session. The audit on 2026-09-12 found both named rules installed and enabled;
their conditions still determine whether a particular task activates them.

## Verification Checklist

When auditing this guide, verify:

- `compile_plan_to_spec` emits one leaf per manifest entry.
- TDD leaves include `additional_skills: ["test-driven-development"]`.
- TDD leaves include label `tdd:required`.
- TDD validation criteria require red, green, refactor/final-green, exact
  command, and supported test-quality audit or unsupported-language fallback
  evidence.
- Developer agents load `gobby:references/development/obligations.md`.
- QA and epic QA agent definitions mention TDD evidence and test-quality
  audit requirements.
- Runtime rule paths still live under
  `src/gobby/install/shared/workflows/rules/tdd-enforcement/`.

## See Also

- [Task Expansion](./task-expansion.md)
- [Test Quality](./test-quality.md)
- [Rules](./rules.md)
- [Variables](./variables.md)
- [Orchestration](./orchestration.md)

_Last verified: 2026-09-12_
