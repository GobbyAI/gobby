<!-- markdownlint-disable MD013 -->

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

Acceptance item IDs are formed by appending `.<n>` to the section's own ID.
The section ID is the prefix, verbatim — no synthetic letters are added.
The parser enforces this with `item_id.startswith(f"{section_id}.")` in
`_build_acceptance_item` (`src/gobby/plans/parser.py`).

Concretely:

- Section `A1` (letter-prefixed) emits `A1.1`, `A1.2`, and so on. The `A` in
  the items is **part of the section ID**, not a synthetic prefix added to
  acceptance items.
- Section `A1.7` emits `A1.7.1`, `A1.7.2`, and so on.
- Section `1.1` (purely numeric) emits `1.1.1`, `1.1.2`, and so on — **no `A`
  prefix**. Items like `A1.1.1` for a section `1.1` are rejected by the
  parser because `A1.1.1` does not start with `1.1.`.

The `A<section>.<n>` shorthand used elsewhere in this contract is read as
"section ID dot n" — for purely numeric sections that means a numeric item
ID with no letter prefix.

Each item names at least one artifact kind: `file`, `symbol`, `test`, or
`behavior`.

```markdown
**Acceptance:**  (under section `A1`, letter-prefixed)

- A1.1 - <prose>. file: `src/module.py`.
- A1.2 - <prose>. symbol: `gobby.module.Symbol`.
- A1.3 - <prose>. test: `tests/test_module.py::test_behavior`.
- A1.4 - <prose>. behavior: "documented behavior" in `docs/contract.md`.
```

```markdown
**Acceptance:**  (under section `1.1`, purely numeric)

- 1.1.1 - <prose>. file: `src/module.py`.
- 1.1.2 - <prose>. symbol: `gobby.module.Symbol`.
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

## Retired Classification Escape Hatches

Grandfather and legacy classification escape hatches are retired. The typed
plan-coverage contract is the single routing source for plan expansion,
coverage evidence, and plan-kind interpretation.

No install-shared state file may override that contract. Reintroducing hidden
grandfather state or legacy classification files under `src/gobby/install/shared/`
is a contract violation and must fail pre-flight validation.

## Task Manifest

Implementation plans carry a single `## M1 Task Manifest` section at the end of
the document. The manifest is the typed bridge between the plan's deliverable
sections and the leaves the deterministic compiler emits at expansion time. The
section heading uses the canonical ID `M1` so it satisfies the section-ID
regex; `kind: manifest` exempts it from the `**Acceptance:**` requirement.

````markdown
## M1 Task Manifest
`kind: manifest`

```yaml
- title: <human-readable title>
  category: <code|config|docs|refactor|test>
  task_type: <feature|bug|chore|...>
  depends_on: [<section-id>, ...]
  validation_criteria: <one-line pass/fail>
  labels:
    - covers:<plan-id>:<section-id>:<item-id>
  implementation_domain: <backend|frontend|fullstack>  # required for code
  assigned_agent: <agent-name>  # optional privileged/manual override
  tdd: <true|false>
  source_section: <section-id>
```
````

Entry schema (one entry per `kind: deliverable` section):

| Field | Type | Notes |
| --- | --- | --- |
| `title` | str | Human-readable title for the synthesized leaf |
| `category` | enum | One of the development-forward categories: `code`, `config`, `docs`, `refactor`, `test` |
| `task_type` | enum | Task-type tag for the synthesized leaf |
| `depends_on` | list[str] | References `source_section` IDs of other manifest entries |
| `validation_criteria` | str | One-line pass/fail |
| `labels` | list[str] | Exactly one `covers:<plan-id>:<section-id>:<item-id>` label per acceptance item in the source section |
| `implementation_domain` | enum|null | Required for `category: code`; one of `backend`, `frontend`, `fullstack` |
| `assigned_agent` | str|null | Optional privileged/manual route override |
| `tdd` | bool | True adds `test-driven-development`, `tdd:required`, and TDD evidence criteria on the leaf |
| `source_section` | str | Must reference a `kind: deliverable` section ID |

`depends_on` values name leaf deliverable dependencies by manifest
`source_section` ID. They must resolve to another manifest entry. Phase IDs
such as `P0` are invalid because phases are not implementation leaves.

### Category/TDD Policy

The single category policy is:

- `code` entries must include `implementation_domain`. Expansion maps
  `backend` to `backend-developer`, `frontend` to `frontend-developer`, and
  `fullstack` to `fullstack-developer` unless a privileged/manual override is
  present.
- `code` and `config` are TDD-eligible. Manifest entries for these categories
  may set `tdd: true`, which makes expansion emit one implementation leaf with
  `additional_skills: ["test-driven-development"]`, label `tdd:required`, and
  validation criteria requiring red, green, refactor/final-green, exact command,
  and test-quality audit evidence.
- `test`, `refactor`, and `docs` are not TDD-eligible. Manifest entries for
  these categories must use `tdd: false`.
- `research`, `planning`, and `manual` are valid direct task categories, but
  expansion manifests reject them.
- `category: test` is valid for standalone test infrastructure,
  characterization, parity, or regression suites that are deliverables in
  their own right. These entries expand as single tasks and must carry their
  own acceptance criteria.
- Filler tasks such as "write tests for X" are rejected when they duplicate
  TDD work required on a `code` or `config` deliverable.

### Parser-Enforced Invariants

When the manifest is present, these invariants are checked regardless of mode:

- Schema-check every entry against the table above.
- Reject `category: code` without `implementation_domain`.
- Reject `tdd: true` unless the entry category is `code` or `config`.
- Every `kind: deliverable` section has exactly one manifest entry referencing
  it via `source_section` (1:1 invariant).
- Every `depends_on` value resolves to another manifest entry's
  `source_section` ID.
- Every `covers:` label resolves to a real acceptance item under the entry's
  `source_section`.
- No orphan manifest entries — every entry's `source_section` resolves to a
  real deliverable section.

A malformed manifest fails the parser in any mode. Only the missing-manifest
behavior differs by mode.

### Parser Modes

`gobby.plans.parser.parse_plan` accepts a `parse_mode` parameter that selects
validation strictness:

| Mode | Manifest | Used by | Behavior |
| --- | --- | --- | --- |
| `parse_mode="draft"` | optional | `validate_plan_file` (planner-side gate run before every adversary spawn); `/gobby plan` Phase 3a; `gobby plan coverage` against drafts | Manifest tolerated absent. If present, schema and 1:1 invariants still apply — a malformed draft manifest still fails. |
| `parse_mode="expansion"` | required | `gobby expand` deterministic compile path; taskless adversary/coordinator post-approval self-check | Raises `PlanParseError("missing manifest")` if the section is absent or any deliverable has no entry. |
| `parse_mode="strict"` (default) | required | callers that want full validation regardless of context | Same strict invariants as `expansion`; default so any caller that omits `parse_mode` keeps full validation. |

The deadlock between "review the plan" and "manifest must exist" is resolved by
construction: the planner-side `validate_plan_file` gate parses in `draft` mode
before each taskless adversary spawn, the adversary then runs qualitative review without
re-parsing, writes the manifest on clean review, self-checks in `expansion`
mode, and downstream `gobby expand` parses in `expansion` against the
now-manifest-bearing plan.

### Manifest-on-Approval Contract

First drafts are narrative-only. The approving `plan-adversary-taskless` run or
interactive coordinator writes `## M1 Task Manifest` after user-approved review.
If the planning agent already supplied complete category and implementation
domain decisions, preserve them. The deterministic manifest emitter is fallback
only for missing manifests or legacy drafts where planning agents did not assign
enough category/domain data.

Sequence on clean review (no blocking findings):

1. Append or repair the `## M1 Task Manifest` section in the plan file.
2. Self-check via `parse_plan(plan_path, parse_mode="expansion")`.
3. On `PlanParseError`, fix the manifest in-place and retry up to 3 times.
4. After the cap is exhausted, return `verdict: needs_review` with the parser
   details. Do not approve.
5. On success, return `verdict: approved` with manifest entry count and whether
   fallback emission was used.

On rejection rounds the adversary MUST NOT edit the plan file — plan edits
between rounds are the parent planner's responsibility. Findings are recorded in
`## V1 Plan Changelog`.

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

## Plan Storage

The `plans` table is the authoritative registry of plan state. The file system
holds plan markdown content; `gobby-plans` MCP tools and the `gobby plans` CLI
are the read/write surfaces for plan metadata. Each row carries:

- `plan_id` — stable identifier matching the plan filename stem.
- `project_id` — owning project UUID.
- `root_task_ref` — the epic seq_num as a string.
- `plan_path` — path relative to the project root.
- `plan_hash` — sha256 of the current plan file content.
- `plan_kind` — one of `implementation`, `strategy`.
- `state` — one of `active`, `archived`.

`plan_kind` controls how the plan participates in coverage verification:

- `implementation` — parsed in strict mode; every active entry MUST have a
  generated manifest at the canonical scoped path, the manifest's `plan_hash`
  MUST match the on-disk plan, and every row MUST resolve to
  `status: covered`. This is the default for new epic plans.
- `strategy` — parsed in permissive mode (canonical headings without `kind:`
  default to `framing`). No manifest is required or permitted; manifests
  pointing at strategy entries fail orphan detection.

Plan archive is system-managed: archiving flips `state` to `archived`, moves
the plan file to `.gobby/plans/completed/`, records `archived_at`, and removes
the managed coverage manifest.

## Table-Row Decomposition

Any `deliverable` section whose body uses a markdown table to enumerate work
items MUST emit one acceptance item per table data row with stable IDs.
Plan-adversary qualitatively rejects deliverables that enumerate work in tables
without per-row acceptance items. This rule closes the #12725 missing-section
failure mode for future plans.
