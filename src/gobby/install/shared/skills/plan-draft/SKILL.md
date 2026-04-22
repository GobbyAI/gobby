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

> Internal methodology skill; invoked via `get_skill` from `/gobby plan` and autonomous agents. Not a user-facing command.

This skill is the single source of truth for **how to write a gobby plan document**.

It is consumed from two places:

- **Interactive:** the `plan` skill loads this to drive `/gobby plan` sessions.
- **Autonomous:** the spawned `planner` agent (`planner.yaml`) loads this as its
  first action so every round of autonomous drafting follows the same format.

A plan written to this methodology is directly consumable by `/gobby expand`
(which creates the task tree) and by `plan-review` (which reviews it adversarially).

---

## Plan Structure

Every plan is one Markdown document. The structure is:

- **Epic title** — `# {Feature Name}` at the top of the file.
- **Overview** — goal + context in one or two short paragraphs.
- **Constraints** — explicit constraints or non-goals.
- **Phases** — one `## Phase N: Name` section per logical grouping of work.
- **Tasks** — one `### N.N Title [category: X]` subsection per atomic unit of work
  under each phase.
- **Dependencies** — inline `(depends: 1.1)` or `(depends: Phase N)` notation on
  task headings.

### Canonical Template

```markdown
# {Epic Title}

## Overview
{Goal and context — 1–2 sentences.}

## Constraints
{Explicit constraints, non-goals, or external requirements.}

## Phase 1: {Phase Name}

**Goal**: {One sentence outcome.}

### 1.1 {Task Title} [category: code]

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

### 1.2 {Task Title} [category: code] (depends: 1.1)

Target: `src/module/other.py`

{Full implementation specification — code examples, behavioral specs, edge cases…}

## Phase 2: {Phase Name}

**Goal**: {One sentence outcome.}

### 2.1 {Task Title} [category: config] (depends: Phase 1)

Target: `config/settings.yaml`

{Full specification including config schema, defaults, validation rules…}

```yaml
settings:
  timeout: 30
  retries: 3
```

## Task Mapping

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|

```

### Dependency Notation

- `(depends: 1.1)` — this task depends on task `1.1` of this plan.
- `(depends: Phase N)` — this task (or the whole phase) depends on phase sub-epic `N`.
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

```
Feature Task
├── [TDD] Write failing tests for feature
├── [IMPL] Implement feature
└── [REF] Refactor with green tests
```

### What the Plan MUST NOT Contain

Drafts must never contain explicit test tasks — expansion inserts them.
Scan and remove any of:

- `"Write tests for..."` / `"Add tests for..."`
- `"Test..."` as a task title prefix
- `"[TDD]..."`, `"[IMPL]..."`, `"[REF]..."` prefixes
- `"Ensure tests pass"` / `"Run tests"`
- `"Add unit tests"` / `"Add integration tests"`
- Any task whose primary verb is `test`

Allowed (these are not test-writing tasks):

- `"Add TestClient fixture"` — test infrastructure, category `test`.
- `"Configure pytest settings"` — configuration, category `config`.

---

## Task Categories

Canonical category list (enum-backed — `src/gobby/storage/tasks/_models.py::VALID_CATEGORIES`):

| Category | TDD sandwich? | Use for |
|----------|---------------|---------|
| `code` | yes | Implementation tasks (requires `validation_criteria` at task-create time) |
| `config` | yes | Configuration file changes |
| `docs` | no | Documentation |
| `refactor` | no | Code restructuring with no behavior change (includes updating existing tests) |
| `test` | no | Test infrastructure (fixtures, helpers — **not** writing test cases) |
| `research` | no | Investigation, no code output expected |
| `planning` | no | Design, architecture |
| `manual` | no | Manual verification (observe output) |

Pick the most specific category that applies. A task that only moves code
around without changing behavior is `refactor`, not `code`.

---

## Phase Heading Syntax

Canonical form is `## Phase N: Name` (colon). Use this in new plans — every
example in this skill follows it.

The `/gobby expand` parser also tolerates:

- `## Phase N — Name` (em-dash)
- `## Phase N – Name` (en-dash)
- `## Phase N - Name` (ASCII hyphen)

Anything else (no separator, unusual punctuation) is **silently skipped** — the
phase will not be recognized and its tasks will be dropped. Always prefer the
canonical colon form.

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
`## Phase` headings.

### Required Structure

```
L1: Root Epic (from plan title)
└── L2: Phase Sub-Epic (from each ## Phase section)
    └── L3: Feature Task (from each ### N.N task heading)
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
2. For each `## Phase N: Name` section:
   - Creates a phase sub-epic under the root.
   - Saves an expansion spec with that phase's `### N.N` tasks.
   - Executes expansion with `tdd=true` — adds [TDD] and [REF] wrappers per phase.
3. Wires cross-phase dependencies (e.g., `depends: Phase N` becomes a dependency
   on the phase sub-epic).

---

## Verification Checklist (run BEFORE presenting the plan)

Before handing the plan off for review or expansion, confirm ALL of the following:

### 1. No Explicit Test Tasks

Scan headings and bullets for forbidden patterns from the TDD Compatibility
section above. Remove any that appear. Report any that were found and removed.

### 2. Dependency Tree Valid

- No circular dependencies (A → B → A is invalid).
- No missing references (if `(depends: 1.1)`, task `1.1` must exist).
- Phase dependencies (`depends: Phase N`) reference existing phases.
- Leaf tasks are concrete implementation work, not meta-tasks like "coordinate
  phase 2".

### 3. Categories Assigned Correctly

Every `### N.N` heading carries a `[category: X]` tag and `X` is one of the
canonical categories above. Fix any missing or unknown category.

### 4. Phase Heading Syntax

Every `## Phase N` heading uses the canonical `## Phase N: Name` form (or one of
the tolerated dash variants). Anything else is silently skipped by expansion.

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
