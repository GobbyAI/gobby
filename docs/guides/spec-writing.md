# Writing Specification Documents

Gobby plans are Markdown specification documents that expand into task trees.
New implementation plans must follow the typed Plan-Coverage Contract so the
parser, expansion QA, and close-time coverage gate can prove every accepted item
has implementation work.

Use the `/gobby plan` skill for interactive drafting. It gathers requirements,
loads the `plan-draft` methodology, writes the plan artifact, and runs the
planner-side validation flow before expansion. Use `gobby build <plan-file>` to
start lifecycle automation from an approved plan file.

For the full contract, read [Plan-Coverage Contract](../contracts/plan-coverage.md).

## Plan Shape

A plan is one Markdown file. The current authoring shape is narrative first:

- `# {Epic Title}` at the top.
- Optional `**Plan ID:** <stable-id>` near the top when the file name cannot
  supply the intended plan ID.
- `## Overview` and `## Constraints` as `kind: framing` sections.
- One `## P<N>: {Phase Name}` section per phase, also `kind: framing`.
- One `### N.M {Task Title} [category: X]` deliverable per atomic unit of work.
- Optional `kind: verification` sections for end-to-end checks.
- Optional `kind: deferred` sections for work deliberately moved out of scope.
- `## Plan Changelog` as `kind: framing` for adversarial revision rounds when
  the planner updates an existing draft after review findings.

Every section heading from `##` through `######` needs a first non-blank
front-matter line:

```markdown
`kind: deliverable | framing | verification | deferred`
```

The parser also recognizes `kind: manifest`, but plan authors do not write that
section. The plan-adversary writes `## M1 Task Manifest` as its final approval
act.

## Canonical Template

```markdown
# Feature Name

## Overview
`kind: framing`

One or two short paragraphs describing the goal and current context.

## Constraints
`kind: framing`

- Non-goal or external constraint.
- Operational constraint.

## P1: Foundation
`kind: framing`

**Goal**: One sentence outcome for the phase.

### 1.1 Add task validation helper [category: code]
`kind: deliverable`

Target: `src/gobby/tasks/validation.py`

Describe the implementation in enough detail for an agent that only receives
this section. Include file paths, symbols, data shapes, edge cases, and any
behavior that must remain stable.

**Acceptance:**

- 1.1.1 - Validation helper rejects empty task titles. file: `src/gobby/tasks/validation.py`.
- 1.1.2 - Empty-title behavior is covered. test: `tests/tasks/test_validation.py::test_rejects_empty_title`.

### 1.2 Add validation CLI coverage [category: test] (depends: 1.1)
`kind: deliverable`

Target: `tests/tasks/test_validation_cli.py`

Add standalone regression coverage for the CLI validation path.

**Acceptance:**

- 1.2.1 - CLI validation error is pinned. test: `tests/tasks/test_validation_cli.py::test_empty_title_error`.

## Verification
`kind: verification`

- `uv run gobby plans validate .gobby/plans/task-123-feature-name.md`
- Focused pytest for touched task validation tests.
```

## Section Kinds

`deliverable` sections describe concrete implementation work. They require an
`**Acceptance:**` block with at least one acceptance item.

`framing` sections carry context, constraints, goals, or non-goals. They do not
carry acceptance items.

`verification` sections summarize end-to-end checks. They do not carry
acceptance items.

`deferred` sections describe work intentionally moved out of the current epic.
They require a typed deferral object:

```yaml
deferral:
  task_ref: "#12345"
  reason: "Why this work is outside the current epic."
  owner: "team-or-agent"
  original_acceptance_items:
    - 2.3.1
```

The referenced task must be open and carry
`deferred-from:<plan-id>:<section-id>` provenance.

## Acceptance Items

Acceptance item IDs are formed by appending `.<n>` to the section ID. The
section ID is the prefix verbatim:

- Section `A1` emits `A1.1`, `A1.2`, and so on.
- Section `A1.7` emits `A1.7.1`, `A1.7.2`, and so on.
- Section `1.1` emits `1.1.1`, `1.1.2`, and so on.

Each acceptance item must name at least one concrete artifact reference. Valid
artifact kinds are `file`, `symbol`, `test`, and `behavior`.

```markdown
**Acceptance:**

- 1.1.1 - Session metadata persists across restarts. file: `src/gobby/sessions/store.py`.
- 1.1.2 - Store lookup is covered. test: `tests/sessions/test_store.py::test_reload_session`.
- 1.1.3 - Public contract is documented. behavior: "session restart behavior" in `docs/guides/sessions.md`.
```

Free-form acceptance without an artifact reference fails parsing.

If an acceptance item names multiple artifacts, the parser uses the first one
as the canonical coverage artifact. Additional `file`, `symbol`, `test`, or
`behavior` references are useful reader context, but they do not replace the
first reference for coverage.

## Dependencies

Use section IDs in dependency annotations:

- `(depends: 1.1)` for a deliverable dependency.
- `(depends: P1)` for a whole phase dependency.

The parser and expansion compiler expect bare section IDs. `depends: Phase 1`
does not resolve.

## Categories And TDD

Every deliverable heading needs a `[category: X]` tag. Current categories are:

| Category | TDD wrapper | Use for |
| --- | --- | --- |
| `code` | yes | Behavior-changing implementation |
| `config` | yes | Configuration changes |
| `docs` | no | Documentation |
| `refactor` | no | Code restructuring with no behavior change |
| `test` | no | Standalone test infrastructure or regression suites |
| `research` | no | Investigation with no code output |
| `planning` | no | Design or architecture work |
| `manual` | no | Manual verification |

`code` and `config` deliverables may use TDD. Expansion emits the TEST, IMPL,
and REF wrapper tasks for those categories. Do not add filler deliverables such
as "write tests for X" when they duplicate the wrapper.

Standalone `category: test` deliverables are valid when they have their own
target, acceptance criteria, and behavior-pinning or test-infrastructure scope.

## Coverage Records

Expansion leaves use structured labels:

```text
covers:<plan-id>:<section-id>:<item-id>
```

Free-form `plan-ref:` labels are ignored by the coverage contract.

Every new epic plan also needs a `.coverage-ledger.yaml` companion in
`.gobby/plans/` before expansion. The ledger records the expected coverage
mapping during the bootstrap period and is rechecked against the managed
coverage manifest at task-stage transitions and close-time validation.

## Task Manifest

Plan authors stop after the narrative sections. The approved plan eventually
carries one manifest section at the end:

```markdown
## M1 Task Manifest
`kind: manifest`
```

The plan-adversary writes that section on approval. Each manifest entry maps
one `kind: deliverable` section to one synthesized leaf task and includes:

| Field | Meaning |
| --- | --- |
| `title` | Human-readable leaf title |
| `category` | One canonical task category |
| `task_type` | Task-type tag |
| `depends_on` | Other manifest `source_section` IDs |
| `validation_criteria` | One-line pass/fail check |
| `labels` | One `covers:<plan-id>:<section-id>:<item-id>` per acceptance item |
| `assigned_agent` | Agent route |
| `tdd` | `true` only for `code` or `config` |
| `source_section` | The source `kind: deliverable` section ID |

Malformed manifests fail parsing. In `draft` mode the manifest may be absent;
in `expansion` and `strict` modes it is required.

## Build And Lifecycle Notes

`gobby build` turns an approved plan into a task stage manifest. Review is
metadata on a stage row: `review_policy`, `reviewer_agent`, attempt counters,
and caps decide whether work is handed to a reviewer. Do not model review as a
separate deliverable or a separate manifest row.

When a plan asks for rule or workflow changes, write against semantic lifecycle
events such as `turn_start` and `turn_end`. Raw `before_agent`, `after_agent`,
and `stop` events are provider/runtime details, not the main authoring API.
Agent termination is separate from those events and still requires
`end_agent_run`.

Docs-category leaf tasks route to `tech-writer`, may run inside the parent
epic's isolation context, and use the development stage's review policy to
decide whether to close directly or submit to doc review.

## Validation

Before presenting or expanding a plan:

1. Confirm every phase heading uses `## P<N>: Name`.
2. Confirm every section has a valid `kind:` line.
3. Confirm every deliverable has a category tag and an acceptance block.
4. Confirm every acceptance item has a valid artifact reference.
5. Confirm dependencies name existing section IDs.
6. Remove filler test tasks duplicated by TDD wrappers.
7. Confirm the bootstrap `.coverage-ledger.yaml` companion exists for new epic
   plans before expansion.
8. Run `uv run gobby plans validate <plan-file>`.

Use `uv run gobby tasks expand validate-plan <plan-file>` only when validating
the task-expansion CLI path itself.

## See Also

- [Task Management Guide](./tasks.md) - Task lifecycle, dependencies, and validation
- [MCP Tools Reference](./mcp-tools.md) - MCP tool API documentation
- [Plan-Coverage Contract](../contracts/plan-coverage.md) - Canonical parser and coverage contract

_Last verified: 2026-05-07_
