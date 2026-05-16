---
name: plan-draft
description: Methodology for drafting a gobby plan document — phases, task format, TDD compatibility, categories, hierarchy, and dependency notation. Use when drafting or revising a plan artifact.
version: "1.0.0"
category: core
internal: true
triggers: plan drafting, plan format, plan specification
metadata:
  gobby:
    audience: all
    depth: 0
---

# plan-draft — Gobby Plan Drafting Methodology

> Internal methodology skill; loaded with `get_skill(name="plan-draft")` from `/gobby plan` and autonomous agents. Not a user-facing command.

This skill is the single source of truth for **how to write a gobby plan document**.

It is consumed from two places:

- **Interactive:** the `plan` skill loads this to drive `/gobby plan` sessions.
- **Autonomous:** the spawned `planner` agent (`planner.yaml`) loads this as its
  first action so every round of autonomous drafting follows the same format.

A plan written to this methodology is directly consumable by `/gobby expand`
(which creates the task tree) and by `plan-review` (which reviews it adversarially).

**Orchestration contract (cross-reference)**: this skill covers *how to write* a
plan; the *spawn / verdict / wake* orchestration around it lives in `plan` (Step
7.0a Anchor-task contract) and `plan-review` (Escalation Policy). Key invariants
that affect drafting authors should know about: the parent session never claims
a task during plan authoring (`.gobby/plans/*.md` is exempt from
`require-task-before-edit`); each adversary or planner round spawns against a
freshly-created per-round anchor task (NOT the planning epic); the parent
fires-and-forgets after each spawn — daemon-wake on the anchor's terminal state
is the only wake signal, no polling or `ScheduleWakeup`. See `plan/SKILL.md`
Step 7 for the full contract.

---

## Plan-Coverage Contract Grammar

New epic plans that will be expanded into implementation leaves MUST follow the
typed Plan-Coverage Contract. The contract lets the parser, expansion QA, and
close-time coverage gate prove that every accepted item becomes covered work.

The canonical heading regex is pinned verbatim here and in the parser:

```regex
^#{2,6}\s+(?:§\s*)?(?P<section_id>(?:\d+(?:\.\d+)*(?:[a-z])?|[A-Z]+[0-9]+(?:\.[0-9]+)*(?:[a-z])?))(?=\s|[).:-]|$)
```

Every section heading at level `##` through `######` carries a first non-blank
front-matter line:

```markdown
`kind: deliverable | framing | verification | deferred`
```

Section kind rules:

- `deliverable` sections describe concrete implementation work and MUST carry
  an `**Acceptance:**` block with one or more numbered acceptance items.
- `framing` sections provide context, scope, or non-goals and carry no
  acceptance items.
- `verification` sections summarize end-to-end acceptance and carry no
  acceptance items of their own.
- `deferred` sections are out-of-scope for this epic and MUST carry a typed
  deferral object pointing at a real open task.

Acceptance item IDs are formed by appending `.<n>` to the section's own ID.
The section ID is the prefix verbatim — **no synthetic letter is added** for
purely numeric sections. The parser enforces this with
`item_id.startswith(f"{section_id}.")` in `_build_acceptance_item`
(`src/gobby/plans/parser.py`).

- Section `A1` (letter-prefixed) emits `A1.1`, `A1.2`, … The `A` is part of
  the section ID, not a prefix added to items.
- Section `A1.7` emits `A1.7.1`, `A1.7.2`, …
- Section `1.1` (purely numeric) emits `1.1.1`, `1.1.2`, … Items like
  `A1.1.1` for section `1.1` are rejected by the parser because `A1.1.1`
  does not start with `1.1.`.

When the contract uses the shorthand `A<section>.<n>`, read it as
"section ID dot n" — for numeric sections the result has no `A`.

```markdown
**Acceptance:**  (under section `A1`, letter-prefixed)

- A1.1 - <prose>. file: `src/module.py`.
- A1.2 - <prose>. symbol: `gobby.module.SomeType`.
- A1.3 - <prose>. test: `tests/test_module.py::test_behavior`.
- A1.4 - <prose>. behavior: "documented behavior" in `docs/contract.md`.
```

```markdown
**Acceptance:**  (under section `1.1`, purely numeric)

- 1.1.1 - <prose>. file: `src/module.py`.
- 1.1.2 - <prose>. symbol: `gobby.module.SomeType`.
```

Each acceptance item MUST name at least one concrete artifact reference. Valid
artifact kinds are `file`, `symbol`, `test`, and `behavior`. The parser uses
the first artifact reference as the canonical coverage artifact; additional
references in the same item are informational.

Deferred sections carry this typed deferral object:

```yaml
deferral:
  task_ref: "#12345"
  reason: "Why this work is outside the current epic."
  owner: "team-or-agent"
  original_acceptance_items:
    - A7.3
```

The referenced task must be open, and it must carry provenance label
`deferred-from:<plan-id>:<section-id>`. A closed task fails the gate.

Expansion leaves MUST emit structured coverage records in this exact shape:

```text
covers:<plan-id>:<section-id>:<item-id>
```

Free-form `plan-ref:` labels are not honored by the coverage contract.

### Authoring Scope: Narrative Only — Never the Manifest

Planner authors **narrative sections only**: `# {Epic Title}`, `## Overview`,
`## Constraints`, `## P<N>: {Phase Name}`, `### N.M {Task Title}` deliverables
with `**Acceptance:**` blocks, `framing` / `verification` / `deferred` sections,
and the `## Plan Changelog` rolling summary (per §2.23). Stop there.

The `## M1 Task Manifest` section is **NOT** part of the planner's surface. It
is written by `plan-adversary` as the final act of approval — the act of
emitting the manifest is what forces the adversary to confront ambiguity it
might otherwise wave through. If a planner draft includes a `## M1 Task
Manifest` section, the adversary will replace it on approval. Drafts presented
for review SHOULD NOT carry one.

Why this split: the planner's job is to fill holes in narrative; the
adversary's job is to commit to a typed, expansion-ready bridge between the
plan and the leaves. Mixing those concerns is what produced the long-context
drift §2.23 fixes. Leave the manifest to the adversary.

See `docs/contracts/plan-coverage.md` (§ "Task Manifest") for the schema and
the adversary-writes-on-approval contract.

### Table-Row Decomposition

Any `deliverable` section whose body uses a markdown table to enumerate work
items MUST emit one acceptance item per table data row with stable IDs, for
example `A7.4.1`, `A7.4.2`, and `A7.4.3`. Plan-adversary qualitatively rejects
deliverables with fewer acceptance items than table data rows. This rule closes
the missing-section failure mode from #12725.

### Whole-Plan Sweep After Findings

After any adversary finding, fix the cited instance and then sweep the whole
plan for the same finding class before resubmitting. If one missing `Target:`
file is found, scan every deliverable for body paths and acceptance `file:` /
`test:` refs missing from its Target/Targets inventory. If one missing consumer
file is found, use code-index usages/blast-radius for the changed symbol or
file and add all direct consumer files that the deliverable owns.

---

## Plan Structure

Every plan is one Markdown document. The structure is:

- **Epic title** — `# {Feature Name}` at the top of the file.
- **Overview** — goal + context in one or two short paragraphs.
- **Constraints** — explicit constraints or non-goals.
- **Phases** — one `## P<N>: Name` section per logical grouping of work
  (letter-prefixed `P1`, `P2`, … so the section_id matches the contract regex
  `^P\d+$`; phases carry `kind: framing`).
- **Tasks** — one `### N.M Title [category: X]` subsection per atomic unit of
  work under each phase (numeric, e.g. `### 1.1`, `### 1.2`, `### 2.1`; tasks
  carry `kind: deliverable`).
- **Dependencies** — inline `(depends: 1.1)` for an intra-phase task or
  `(depends: P<N>)` for the whole phase, on task headings. Use the bare
  section_id — `(depends: Phase 1)` does NOT resolve.

### Canonical Build Stages

Build turns the approved plan into a stage manifest. The active registry stages,
in manifest order, are:

1. `ideation`
2. `research`
3. `architecture`
4. `prd`
5. `planning`
6. `expansion`
7. `development`
8. `holistic_qa`
9. `pr`
10. `merge`

Review behavior is metadata on these surviving rows through `review_policy`,
`reviewer_agent`, attempt counters, and caps. Do not model review work as
separate manifest rows when drafting a plan; route it through the surviving
stage that owns the review.

### Canonical Template

Each section heading carries a `` `kind: ...` `` annotation on the next
non-blank line. Phases are `kind: framing`; task subsections are
`kind: deliverable` and require an `**Acceptance:**` block. Without these
annotations the parser silently drops the section.

~~~markdown
# {Epic Title}

## Overview
`kind: framing`

{Goal and context — 1–2 sentences.}

## Constraints
`kind: framing`

{Explicit constraints, non-goals, or external requirements.}

## P1: {Phase Name}
`kind: framing`

**Goal**: {One sentence outcome.}

### 1.1 {Task Title} [category: code]
`kind: deliverable`

Target: `src/module/file.py`

{Full implementation specification for this task. Everything here becomes the
subtask description during expansion — the implementing agent sees ONLY this
section.}

Include:
- File paths to create/modify
- Code examples (classes, functions, schemas)
- Behavioral specs and edge cases
- SQL migrations, config snippets, etc.

```python
class Example(Base):
    """Show the shape of the solution with concrete code."""
    __tablename__ = "examples"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)

    def validate(self) -> bool:
        return len(self.name) > 0
```

**Acceptance:**

- 1.1.1 - Example model exists. file: `src/module/file.py`.
- 1.1.2 - Validation rejects empty names. test: `tests/test_module.py::test_validate_empty`.

### 1.2 {Task Title} [category: code] (depends: 1.1)
`kind: deliverable`

Target: `src/module/other.py`

{Full implementation specification — code examples, behavioral specs, edge cases…}

**Acceptance:**

- 1.2.1 - {prose}. file: `src/module/other.py`.

## P2: {Phase Name}
`kind: framing`

**Goal**: {One sentence outcome.}

### 2.1 {Task Title} [category: config] (depends: P1)
`kind: deliverable`

Target: `config/settings.yaml`

{Full specification including config schema, defaults, validation rules…}

```yaml
settings:
  timeout: 30
  retries: 3
```

**Acceptance:**

- 2.1.1 - {prose}. file: `config/settings.yaml`.

## Task Mapping

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

~~~

### Dependency Notation

- `(depends: 1.1)` — this task depends on task `1.1` of this plan.
- `(depends: P<N>)` — this task (or the whole phase) depends on phase
  sub-epic `P<N>`. Use the bare section_id of the phase, not the literal
  word "Phase" — `(depends: Phase 1)` does not resolve.
- Dependencies are resolved by `/gobby expand` using `parent_task_id` and
  `add_dependency`; do not try to pre-create task refs yourself.

---

## Plan Content = Subtask Descriptions

**Everything under a `### N.N` task heading becomes that subtask's description during expansion.**

When `/gobby expand` processes the plan, it extracts each `### N.N` section and
uses its full content as the subtask description. The implementing agent only
sees its own subtask — it does **not** have access to the full plan document.

**Each task section must be self-contained:**

- File paths to create or modify
- Code examples (classes, functions, method signatures)
- Config snippets, SQL migrations, YAML schemas
- ASCII diagrams, data-flow descriptions
- Behavioral specs and edge cases
- Everything the implementing agent needs to do the work

**Do NOT defer detail.** If you know the model fields, list them. If you know
the SQL schema, write it. If you know the function signature, include it.
Brief bullets like "implement the user model" force the implementing agent to
guess — and it will guess wrong.

---

## TDD Compatibility (IMPORTANT)

When `/gobby expand` processes the plan, it applies TDD automatically to each
`code`/`config` task.

### TDD Triplet Pattern

Each feature task (category `code` or `config`) gets expanded into three children:

- **[TDD]** — write failing tests first
- **[IMPL]** — make tests pass
- **[REF]** — refactor while keeping tests green

```text
Feature Task
├── [TDD] Write failing tests for feature
├── [IMPL] Implement feature
└── [REF] Refactor with green tests
```

### What the Plan MUST NOT Contain

Drafts must never contain filler test tasks that duplicate TDD wrappers —
expansion inserts those for every `code` and `config` deliverable. Scan and
remove any of:

- `"Write tests for..."` / `"Add tests for..."`
- `"Test..."` as a task title prefix
- `"[TDD]..."`, `"[IMPL]..."`, `"[REF]..."` prefixes
- `"Ensure tests pass"` / `"Run tests"`
- `"Add unit tests"` / `"Add integration tests"`
- Any task whose only purpose is testing a sibling `code`/`config` deliverable

Allowed standalone `category: test` deliverables have their own acceptance
criteria and are not substitutes for a sibling TDD wrapper:

- `"Add TestClient fixture"` — test infrastructure, category `test`.
- `"Add cross-backend parity regression suite"` — behavior-pinning suite,
  category `test`.
- `"Configure pytest settings"` — configuration, category `config`.

---

## Task Categories

Expansion manifest category list:

| Category | TDD sandwich? | Use for |
|----------|---------------|---------|
| `code` | yes | Implementation tasks (requires `validation_criteria` at task-create time) |
| `config` | yes | Configuration file changes |
| `docs` | no | Documentation |
| `refactor` | no | Code restructuring with no behavior change (includes updating existing tests) |
| `test` | no | Standalone test infrastructure, parity, characterization, or regression suites with their own acceptance criteria |
| `research` | no | Investigation, no code output expected |
| `planning` | no | Design, architecture |

Pick the most specific category that applies. A task that only moves code
around without changing behavior is `refactor`, not `code`.
`manual` remains valid for direct task creation outside expansion manifests; in
plans that expand into tasks, live/manual verification belongs in `test`.

---

## Phase Heading Syntax

Canonical form is `## P<N>: {Phase Name}` — letter-prefixed `P` plus the phase
number, matching the contract phase regex
`_CONTRACT_PHASE_ID_RE = re.compile(r"^P(?P<number>\d+)$")` at
`src/gobby/tasks/expansion/_common.py:219`.

Examples that work:

- `## P1: Setup` (colon separator)
- `## P1 Setup` (space separator — the canonical-heading regex's lookahead
  accepts whitespace, `).:-`, or end-of-line after the section_id)
- `## P2: Implementation`

Examples that **silently fail** (parser extracts no section_id, or extracts
the wrong one — phase will not be recognized and the whole phase plus its
tasks will be dropped at expansion time):

- `## Phase 1: Setup` — the literal word "Phase" prevents the section_id
  regex from matching; `_CONTRACT_PHASE_ID_RE` would not see `P<N>` here.
- `## Phase N — Name` and dash variants — same problem.
- `## 1: Setup` — extracts section_id `"1"` (numeric) which does NOT match
  the phase regex `^P\d+$`. The validator returns `phase_count: 0` and the
  expansion compiler cannot anchor TDD wrappers.

Always prefer `## P<N>: Name`. Pair every phase heading with `` `kind: framing` ``
on the next non-blank line — without it the parser treats the section as
unannotated and drops it.

---

## Task Granularity Guidelines

Each task should be:

- **Atomic** — completable in one AI session.
- **Testable** — clear pass/fail criteria.
- **Verb-led** — starts with an action verb: Add, Create, Implement, Update, Remove, Extract.
- **Scoped** — references specific files/functions when possible.
- **Self-contained** — every section contains ALL implementation detail the
  implementing agent needs (code examples, schemas, file paths, behavioral
  specs).

Good: `"Add TaskEnricher class to src/gobby/tasks/enrich.py"`
Bad: `"Implement enrichment"` (too vague — the agent has to guess)

---

## Task Hierarchy

Plans with multiple phases **must** produce a hierarchical task tree, not a flat
list. `/gobby expand` creates this hierarchy automatically from the plan's
`## P<N>` headings.

### Required Structure

```
L1: Root Epic (from plan title)
└── L2: Phase Sub-Epic (from each ## P<N> section)
    └── L3: Feature Task (from each ### N.M task heading)
        ├── [TDD] Write failing tests     ← TDD sandwich (auto-generated)
        ├── [IMPL] Implement feature      ← TDD sandwich (auto-generated)
        └── [REF] Refactor with green tests ← TDD sandwich (auto-generated)
```

### Why Phases Must Be Sub-Epics

- **TDD sandwiches are per-phase** — each phase gets its own [TDD]/[REF] wrapper.
- **Parallel dispatch** — phases with no cross-dependencies can be dispatched independently.
- **Progress tracking** — phase completion is visible without scanning 30+ flat tasks.
- **Dependency scoping** — intra-phase deps are local; cross-phase deps are explicit.

### How `/gobby expand` Handles Phases

1. Creates the root epic from `# Title`.
2. For each `## P<N>: Name` section:
   - Creates a phase sub-epic under the root.
   - Saves an expansion spec with that phase's `### N.M` tasks.
   - Executes expansion with `tdd=true` — adds [TDD] and [REF] wrappers per phase.
3. Wires cross-phase dependencies (e.g., `depends: P<N>` becomes a dependency
   on the phase sub-epic).

---

## Verification Checklist (run BEFORE presenting the plan)

Before handing the plan off for review or expansion, confirm ALL of the following:

After this narrative checklist passes, the orchestrating `plan` workflow MUST
run `uv run gobby plans validate <plan-file>` before adversary review or
expansion. Use `gobby tasks expand validate-plan` only from task-expansion
workflows or when debugging the expansion CLI path itself.

### 1. No Explicit Test Tasks

Scan headings and bullets for forbidden duplicate TDD-wrapper patterns from the
TDD Compatibility section above. Remove filler "write tests for X" tasks that
belong inside the generated wrapper. Keep standalone `category: test`
deliverables when they have their own target, acceptance criteria, and
behavior-pinning or test-infrastructure purpose.

### 2. Dependency Tree Valid

- No circular dependencies (A → B → A is invalid).
- No missing references (if `(depends: 1.1)`, task `1.1` must exist).
- Phase dependencies (`depends: P<N>`) reference existing phases by their
  letter-prefixed section_id. `(depends: Phase 1)` does not resolve.
- Leaf tasks are concrete implementation work, not meta-tasks like "coordinate
  phase 2".

### 3. Categories Assigned Correctly

Every `### N.M` heading carries a `[category: X]` tag and `X` is one of the
canonical categories above. Fix any missing or unknown category.

### 4. Phase Heading Syntax

Every phase heading uses the canonical `## P<N>: Name` form (letter-prefixed
section_id matching `^P\d+$`). Headings like `## Phase 1: Name`, `## 1: Name`,
or dash-separator variants are **silently dropped** by the parser — the
phase will not be recognized and its tasks will be lost at expansion. Pair
each phase heading with `` `kind: framing` `` on the next non-blank line.

### 5. Sections Are Self-Contained

Spot-check three `### N.N` sections: does each one contain enough detail (file
paths, code examples, behavioral specs) that an agent who sees ONLY that section
can do the work without outside context?

### Verification Output

Report:

```
Plan Verification:
✓ No explicit test tasks found
✓ Dependency tree is valid (no cycles, all refs exist)
✓ Categories assigned correctly
✓ Phase headings use canonical syntax
✓ Task sections are self-contained

Ready for review.
```

Or, if issues were fixed:

```
Plan Verification:
✗ Found 2 explicit test tasks (removed):
  - "Add tests for user authentication" → REMOVED
  - "Ensure all tests pass" → REMOVED
✓ Dependency tree is valid
✓ Categories assigned correctly
✓ Phase headings use canonical syntax
✓ Task sections are self-contained

Plan updated. Ready for review.
```

If any check still fails after attempted fixes, **do not present the plan yet** —
revise until every check passes.

---

## Revision Round Mandate

Planner revision rounds run in fresh context. Read only the current plan file,
the cumulative `## Plan Changelog`, and the latest `## Adversary Findings —
Round N` supplied by the coordinator.

Apply surgical fixes: missing acceptance items, ambiguous wording, stale file
paths, missing dependency annotations, and contradictions with the codebase.
Do not redesign the plan in response to adversary findings. When a finding
requires rejecting the premise or re-engineering a section, call
`escalate_task(reason="needs_human:premise_disagreement:<section_id>:<details>")`.

After a revision, append one concise bullet to `## Plan Changelog` describing
the surgical fixes for that round, then resubmit with one atomic
`submit_for_review(task_id=..., stage_name="planning", review_notes=...)` call. That transition
clears `planning-current-verdict:rejected`; do not clear the label separately.

Planners author narrative sections only. The `## M1 Task Manifest` is emitted
by `plan-adversary` on approval.
