# Task structure and categories

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
8. `epic_qa`
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

Targets:
- `src/module/file.py::Example`
- `src/module/file.py::Example.validate`
- `tests/test_module.py::test_validate_empty`

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

Target: `src/module/other.py::build_other`

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

Expansion no longer creates default `[TDD]` / `[IMPL]` / `[REF]` wrapper
children for new plans. Each manifest entry emits one implementation leaf.
When the manifest sets `tdd: true`, that leaf receives:

- `additional_skills: ["test-driven-development"]`
- label `tdd:required`
- validation criteria requiring red, green, refactor/final-green, exact test
  command, and test-quality audit evidence

### What the Plan MUST NOT Contain

Drafts must never contain filler test tasks that duplicate TDD-required
implementation leaves. Scan and remove any of:

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

| Category | TDD eligible? | Use for |
|----------|---------------|---------|
| `code` | yes | Implementation tasks; manifest entries require `implementation_domain` |
| `config` | conditional | Configuration changes; use `tdd: true` only for executable behavior |
| `docs` | no | Documentation |
| `refactor` | no | Code restructuring with no behavior change (includes updating existing tests) |
| `test` | no | Standalone test infrastructure, parity, characterization, or regression suites with their own acceptance criteria |

For `category: code`, the manifest must include
`implementation_domain: backend | frontend | fullstack`. The domain routes the
leaf to `backend-developer`, `frontend-developer`, or `fullstack-developer`.

Pick the most specific category that applies. A task that only moves code
around without changing behavior is `refactor`, not `code`.
`research`, `planning`, and `manual` remain valid for direct task creation
outside expansion manifests. In plans that expand into tasks, discovery and
design must already be resolved; live/manual verification belongs in `test`.

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
  expansion compiler cannot build the phase hierarchy.

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
```

### Why Phases Must Be Sub-Epics

- **One leaf per manifest entry** — TDD is enforced by task metadata and skills.
- **Parallel dispatch** — phases with no cross-dependencies can be dispatched independently.
- **Progress tracking** — phase completion is visible without scanning 30+ flat tasks.
- **Dependency scoping** — intra-phase deps are local; cross-phase deps are explicit.

### How `/gobby expand` Handles Phases

1. Creates the root epic from `# Title`.
2. For each `## P<N>: Name` section:
   - Creates a phase sub-epic under the root.
   - Saves an expansion spec with that phase's `### N.M` tasks.
   - Emits one leaf per manifest entry.
3. Wires cross-phase dependencies (e.g., `depends: P<N>` becomes a dependency
   on the phase sub-epic).

---
