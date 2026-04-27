# Plan-Coverage Contract

This page is the canonical reading order for the plan-coverage contract:

1. `CLAUDE.md` for repo-wide agent requirements.
2. `src/gobby/install/shared/skills/plan/SKILL.md` for interactive authoring.
3. `src/gobby/install/shared/skills/plan-draft/SKILL.md` for the typed grammar.
4. `src/gobby/install/shared/skills/plan-review/SKILL.md` for adversarial review.
5. `src/gobby/install/shared/skills/expand/SKILL.md` for expansion obligations.
6. `src/gobby/install/shared/workflows/agents/expansion-qa.yaml` for QA gating.

Parser and coverage implementation surfaces live under `gobby.plans.parser`,
`gobby plan coverage`, and the evidence resolver used by the coverage matrix.

## Canonical Heading Regex

```regex
^#{2,6}\s+(?:§\s*)?(?P<section_id>(?:\d+(?:\.\d+)*(?:[a-z])?|[A-Z]+[0-9]+(?:\.[0-9]+)*(?:[a-z])?))(?=\s|[).:-]|$)
```

The regex accepts heading levels `##` through `######`, optional `§`, numeric
section IDs of any depth, alpha-prefixed IDs such as `A10`, and an optional
letter suffix on the final segment.

## Section Kinds

Every section carries first-line front matter:

```markdown
`kind: deliverable | framing | verification | deferred`
```

- `deliverable` sections require an `**Acceptance:**` block with at least one
  acceptance item.
- `framing` sections carry context or non-goals and no acceptance items.
- `verification` sections summarize end-to-end checks and no acceptance items.
- `deferred` sections require a typed deferral object.

## Acceptance Items

Acceptance IDs use dotted suffixes rooted in the section ID. Section `A1`
emits `A1.1`, `A1.2`, and so on. Section `A1.7` emits `A1.7.1`, `A1.7.2`,
and so on.

Each item names at least one artifact kind: `file`, `symbol`, `test`, or
`behavior`.

```markdown
**Acceptance:**

- A1.1 - <prose>. file: `src/module.py`.
- A1.2 - <prose>. symbol: `gobby.module.Symbol`.
- A1.3 - <prose>. test: `tests/test_module.py::test_behavior`.
- A1.4 - <prose>. behavior: "documented behavior" in `docs/contract.md`.
```

## Deferrals

Typed deferral object:

```yaml
deferral:
  task_ref: "#12345"
  reason: "Why this work is outside the current epic."
  owner: "team-or-agent"
  original_acceptance_items:
    - A7.3
```

The referenced task must be open and carry
`deferred-from:<plan-id>:<section-id>` provenance. A closed task fails the gate.

## Coverage Records

Leaves emit structured labels:

```text
covers:<plan-id>:<section-id>:<item-id>
```

Free-form `plan-ref:` labels are not honored; only structured `covers:` labels
are valid coverage signal.

## Coverage CLI

```bash
gobby plan coverage \
  --plan <path> \
  --plan-id <id> \
  --plan-hash <sha256> \
  --task-tree <db|jsonl|path> \
  [--root-task <ref>] \
  [--project-id <id>] \
  [--matrix-file <path>] \
  [--evidence <kind>] \
  [--manifest <path>] \
  [--regenerate]
```

Required flags: `--plan`, `--plan-id`, `--plan-hash`, `--task-tree`.

Optional flags: `--root-task`, `--project-id`, `--matrix-file`, `--evidence`,
`--manifest`, `--regenerate`.

Exit codes: `0`, `2`, `3`, `4`, `5`, `6`, `7`, `8`.

Evidence kinds: `commits | task-diff | worktree-diff | coverage-matrix | none`.

## Bootstrap Ledger

Every new epic plan ships a `.coverage-ledger.yaml` companion file,
adversary-reviewed before expansion, until the contract tooling is mature. The
ledger enumerates deliverable acceptance items and expected implementation
leaves so close-time validation can compare manifest rows against the plan.

## Grandfathered Epics

The `.grandfathered` mechanism is reserved for already-merged epics. Additions
require a paired `# remove-by: <task-ref>` annotation and an open task. New
plans must use the normal contract instead of being grandfathered.

## Table-Row Decomposition

Any `deliverable` section whose body uses a markdown table to enumerate work
items MUST emit one acceptance item per table data row with stable IDs.
Plan-adversary qualitatively rejects deliverables that enumerate work in tables
without per-row acceptance items. This rule closes the #12725 missing-section
failure mode for future plans.
