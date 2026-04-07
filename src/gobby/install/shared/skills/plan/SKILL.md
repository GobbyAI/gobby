---
name: plan
description: This skill should be used when the user asks to "/gobby plan", "create plan", "plan feature", "write specification". Guide users through structured specification planning. Does NOT create tasks - use /gobby expand for that.
version: "1.0.0"
category: core
triggers: plan, specification, requirements
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby plan - Implementation Planning Skill

Guide users through structured requirements gathering and specification writing.
**This skill creates the plan document only.** Use `/gobby expand <plan-file>` to create tasks.

## Workflow Overview

1. **Requirements Gathering** - Ask questions to understand the feature
2. **Draft Plan** - Write structured plan document
3. **Plan Verification** - Check for TDD anti-patterns and dependency issues
4. **User Approval** - Present plan for review
5. **Hand off to /gobby expand** - User runs `/gobby expand` on the plan file

## Step 0: **REQUIRED** ENTER PLAN MODE

Before creating any plan, you must enter Claude Code's plan mode to explore the codebase
and design the implementation approach.

**How to enter**: Use the `EnterPlanMode` tool or respond with a planning-focused message
that triggers plan mode. Plan mode allows you to read files and design without making edits.

**Why required**: Plan creation requires understanding existing code patterns, architecture
constraints, and dependencies before proposing new work.

## Step 1: Requirements Gathering

Ask the user:
1. "What is the name/title for this feature or project?"
2. "What is the high-level goal? (1-2 sentences)"
3. "Are there any constraints or requirements I should know about?"
4. "What are the unknowns or risks?"

## Step 2: Draft Plan Structure

Create a plan with:
- **Epic title**: The overall feature name
- **Phases**: Logical groupings of work (e.g., "Foundation", "Core Implementation", "Polish")
- **Tasks**: Atomic units of work under each phase
- **Dependencies**: Which tasks block which (use notation: `depends: #N` or `depends: Phase N`)

## Step 3: Write Plan Document

Write to `.gobby/plans/{kebab-name}.md`:

```markdown
# {Epic Title}

## Overview
{Goal and context from Step 1}

## Constraints
{Constraints from Step 1}

## Phase 1: {Phase Name}

**Goal**: {One sentence outcome}

### 1.1 {Task Title} [category: code]

Target: `src/module/file.py`

{Full implementation specification for this task. Everything here becomes the
subtask description during expansion — the implementing agent sees ONLY this section.}

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

{Full implementation specification — code examples, behavioral specs, edge cases...}

## Phase 2: {Phase Name}

**Goal**: {One sentence outcome}

### 2.1 {Task Title} [category: config] (depends: Phase 1)

Target: `config/settings.yaml`

{Full specification including config schema, defaults, validation rules...}

```yaml
settings:
  timeout: 30
  retries: 3
  # Show exact config structure the agent should produce
```

## Task Mapping

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
```

**Dependency Notation:**
- Use `(depends: 1.1)` or `(depends: Phase N)` on task headings
- Dependencies are resolved when tasks are created via `create_task` with `parent_task_id`

## Plan Content = Subtask Descriptions

**Everything under a `### N.N` task heading becomes that subtask's description during expansion.**

When `/gobby expand` processes your plan, it extracts each `### N.N` section and uses its
full content as the subtask description. The implementing agent only sees its own subtask —
it does NOT have access to the full plan document.

**Each task section must be self-contained:**
- File paths to create or modify
- Code examples (classes, functions, method signatures)
- Config snippets, SQL migrations, YAML schemas
- ASCII diagrams, data flow descriptions
- Behavioral specs and edge cases
- Everything the implementing agent needs to do the work

**Do NOT defer detail.** If you know the model fields, list them. If you know the SQL
schema, write it. If you know the function signature, include it. Brief bullets like
"implement the user model" force the implementing agent to guess — and it will guess wrong.

## Step 4: Plan Verification (REQUIRED)

Before presenting to the user, verify the plan does NOT contain TDD anti-patterns:

### Check 1: No Explicit Test Tasks

Scan for tasks that should NOT exist (TDD sandwich creates these automatically):

**FORBIDDEN patterns - remove these if found:**
- `"Write tests for..."` or `"Add tests for..."`
- `"Test..."` as task title prefix
- `"[TDD]..."` or `"[IMPL]..."` or `"[REF]..."`
- `"Ensure tests pass"` or `"Run tests"`
- `"Add unit tests"` or `"Add integration tests"`
- Any task with `test` as the primary verb

**ALLOWED (these are fine):**
- `"Add TestClient fixture"` (not a test task, but test infrastructure)
- `"Configure pytest settings"` (configuration, not test writing)

### Check 2: Dependency Tree Validation

Verify the dependency structure is valid:

1. **No circular dependencies**: Task A → B → A is invalid
2. **No missing dependencies**: If Task B depends on Task A, Task A must exist
3. **Phase dependencies are valid**: `depends: Phase N` must reference an existing phase
4. **Leaf tasks are implementation work**: Bottom-level tasks should be concrete work, not meta-tasks

### Check 3: Task Categorization

Ensure each task has a valid category:
- `code` - Implementation tasks (gets TDD triplets)
- `config` - Configuration changes (gets TDD triplets)
- `docs` - Documentation tasks (no TDD)
- `refactor` - Refactoring tasks, including updating existing tests (no TDD)
- `test` - Test infrastructure (no TDD)
- `research` - Investigation tasks (no TDD)
- `planning` - Architecture/design (no TDD)
- `manual` - Manual verification (no TDD)

### Verification Output

After verification, report:
```
Plan Verification:
✓ No explicit test tasks found
✓ Dependency tree is valid (no cycles, all refs exist)
✓ Categories assigned correctly

Ready for user approval.
```

Or if issues found:
```
Plan Verification:
✗ Found 2 explicit test tasks (removed):
  - "Add tests for user authentication" → REMOVED
  - "Ensure all tests pass" → REMOVED
✓ Dependency tree is valid
✓ Categories assigned correctly

Plan updated. Ready for user approval.
```

## Step 5: User Approval & Handoff

Present the plan to the user:
- Show the full plan document
- Show verification results
- Ask: "Does this plan look correct? Would you like any changes?"
- Make changes if requested

Once approved, tell the user:
```
Plan approved! To create tasks from this plan, run:

/gobby expand <plan-file-path>

This will:
1. Create a root epic from the plan title
2. Create phase sub-epics under the root (one per ## Phase section)
3. For each phase, create feature tasks with TDD sandwiches
4. Wire up intra-phase and cross-phase dependencies
```

**This skill ends here.** Task creation is handled by `/gobby expand`.

## Task Hierarchy (IMPORTANT)

Plans with multiple phases **must** produce a hierarchical task tree, not a flat list.
`/gobby expand` creates this hierarchy automatically from the plan's `## Phase` headings.

### Required Structure

```
L1: Root Epic (from plan title)
└── L2: Phase Sub-Epic (from each ## Phase section)
    └── L3: Feature Task (from each ### N.N task heading)
        ├── [TEST] Write failing tests    ← TDD sandwich (auto-generated)
        ├── [IMPL] Implement feature      ← TDD sandwich (auto-generated)
        └── [REF] Refactor with green tests ← TDD sandwich (auto-generated)
```

### Why Phases Must Be Sub-Epics

- **TDD sandwiches are per-phase**, not per-epic — each phase gets its own [TEST]/[REF] wrapper
- **Parallel dispatch**: phases with no cross-dependencies can be dispatched independently
- **Progress tracking**: phase completion is visible without scanning 30+ flat tasks
- **Dependency scoping**: intra-phase deps are local, cross-phase deps are explicit

### How /gobby expand Handles Phases

When expand processes a plan file:

1. Creates root epic from `# Title`
2. For each `## Phase N: Name` section:
   - Creates a sub-epic under the root
   - Saves an expansion spec with that phase's `### N.N` tasks
   - Executes expansion with `tdd=true` — adds [TEST] and [REF] wrappers per phase
3. Wires cross-phase dependencies (e.g., `depends: Phase N` becomes dependency on phase sub-epic)

### Manual Hierarchy Creation (when expand pipeline is unavailable)

If the expand pipeline fails, create the hierarchy manually:

```python
# 1. Create root epic
epic = call_tool("gobby-tasks", "create_task", {
    "title": "Plan Title", "task_type": "epic", "category": "code"
})

# 2. For each phase, create sub-epic
phase1 = call_tool("gobby-tasks", "create_task", {
    "title": "Phase 1: Foundation",
    "task_type": "epic", "category": "code",
    "parent_task_id": epic["ref"]
})

# 3. Save expansion spec per phase (only that phase's tasks)
call_tool("gobby-tasks-ops", "save_expansion_spec", {
    "task_id": phase1["ref"],
    "spec": {"plan_file": "path/to/plan.md", "subtasks": [/* phase 1 tasks only */]}
})

# 4. Execute expansion per phase with TDD
call_tool("gobby-tasks-ops", "execute_expansion", {
    "parent_task_id": phase1["ref"], "tdd": true
})

# 5. Wire cross-phase dependencies
call_tool("gobby-tasks", "add_dependency", {
    "task_id": phase2_first_task, "depends_on": phase1["ref"]
})
```

---

## Plan Format Reference (for /gobby expand)

The following sections describe what `/gobby expand` expects and how it will process your plan.

## Task Granularity Guidelines

Each task should be:
- **Atomic**: Completable in one AI session
- **Testable**: Has clear pass/fail criteria
- **Verb-led**: Starts with action verb (Add, Create, Implement, Update, Remove)
- **Scoped**: References specific files/functions when possible
- **Self-contained**: Each task section contains ALL implementation detail the agent needs — code examples, schemas, file paths, and behavioral specs written directly in the section

Good: "Add TaskEnricher class to src/gobby/tasks/enrich.py"
Bad: "Implement enrichment" (too vague)

## TDD Compatibility (IMPORTANT)

When `/gobby expand` processes your plan, it applies TDD to each code/config task automatically.

### TDD Triplet Pattern

Each feature task (category: code) gets expanded into three children:
- **[TDD]** - Write failing tests first
- **[IMPL]** - Make tests pass
- **[REF]** - Refactor while keeping tests green

```
Feature Task
├── [TDD] Write failing tests for feature
├── [IMPL] Implement feature
└── [REF] Clean up, verify tests pass
```

### Task Categories

Valid categories (from `src/gobby/storage/tasks.py`):
- `code` - Implementation tasks (gets TDD triplets)
- `config` - Configuration changes (gets TDD triplets)
- `docs` - Documentation tasks (no TDD)
- `refactor` - Refactoring tasks, including updating existing tests (no TDD)
- `test` - Test infrastructure tasks (fixtures, helpers) (no TDD)
- `research` - Investigation tasks (no TDD)
- `planning` - Architecture/design tasks (no TDD)
- `manual` - Manual functional testing (observe output) (no TDD)

### What /gobby plan Creates vs What /gobby expand Creates

**Your plan document should contain:**
- Feature tasks with implied `category: code` or `category: config`
- Documentation tasks with implied `category: docs`
- Clear phase structure and dependencies

**Your plan should NOT contain:**
- `[TDD]`, `[IMPL]`, `[REF]` prefixed tasks
- "Write tests for: ..." tasks
- "Ensure tests pass" tasks
- Separate test tasks alongside implementation

**When you run `/gobby expand <plan-file>`:**

```
Input: .gobby/plans/memory-v3.md

Output task tree:
#100 [epic] Memory V3 Backend                           L1 (root epic)
├── #101 [epic] Phase 1: Protocol Layer                  L2 (phase sub-epic)
│   ├── [TEST] Phase 1: Write failing tests              L3 (TDD sandwich)
│   ├── #102 [task] Create protocol.py                   L3 (from plan § 1.1)
│   │   └─ validation: Protocol class exists with required methods
│   ├── #103 [task] Create backends/__init__.py          L3 (from plan § 1.2)
│   │   └─ validation: Factory function works
│   └── [REF] Phase 1: Refactor with green tests         L3 (TDD sandwich)
├── #104 [epic] Phase 2: Configuration                   L2 (phase sub-epic)
│   ├── [TEST] Phase 2: Write failing tests              L3 (TDD sandwich)
│   ├── #105 [task] Add config schema                    L3 (from plan § 2.1)
│   │   └─ validation: Config loads and validates
│   └── [REF] Phase 2: Refactor with green tests         L3 (TDD sandwich)
```

**NOTE**: Each phase gets its own TDD [TEST]/[REF] sandwich. `/gobby expand` preserves
the full plan section content in each task description. Cross-phase dependencies are wired
between phase sub-epics (e.g., Phase 2 depends on Phase 1 sub-epic).

## Example Usage

```
User: /gobby plan
Agent: "What feature would you like to plan?"
User: "Add dark mode support to the app"
Agent: [Asks clarifying questions]
Agent: [Writes plan to .gobby/plans/dark-mode.md]
Agent: [Runs verification - checks for TDD anti-patterns]
Agent: "Here's the plan. Does this look correct?"
User: "Yes, looks good"
Agent: "Plan approved! To create tasks, run:
        /gobby expand .gobby/plans/dark-mode.md"

User: /gobby expand .gobby/plans/dark-mode.md
Agent: [Creates root epic, phase sub-epics, feature tasks with TDD sandwiches per phase]
Agent: "Created 3 phases, 12 feature tasks under epic #47 with TDD and validation criteria."
```

## Optional: Workflow-Enforced Planning

For stricter enforcement of the planning process with step gates and tool restrictions,
you can activate the `plan-expansion` workflow instead of following this skill manually.

### When to Use the Workflow

Use the workflow when you want:
- **Hard step gates** - Can't proceed to task creation without approval
- **Tool restrictions** - Edit/Write blocked during discovery and gather phases
- **Loop enforcement** - Expansion loop can't be skipped or abandoned
- **State persistence** - Workflow state survives context compaction

### How to Activate

After gathering requirements (Step 1), activate the workflow:

```python
call_tool("gobby-workflows", "activate_workflow", {
    "name": "plan-expansion",
    "variables": {
        "context_analyzed": false,  # Start from discovery
        "apc_choice": null
    }
})
```

Or skip discovery if you've already analyzed the codebase:

```python
call_tool("gobby-workflows", "activate_workflow", {
    "name": "plan-expansion",
    "step": "gather",  # Start from requirements elicitation
    "variables": {
        "context_analyzed": true
    }
})
```

### Workflow Steps

1. **discover** - Analyze existing context (blocks Edit/Write)
2. **gather** - A/P/C elicitation menu (blocks Edit/Write)
3. **draft_plan** - Write plan document (only .gobby/plans/ allowed)
4. **verify_plan** - Check structure and dependencies
5. **create_hierarchy** - Create epic → phase → task structure
6. **expand_loop** - Auto-expand feature tasks with TDD
7. **cleanup** - Evaluate tree, fix deps, identify duplicates, offer cleanup
8. **verify_tasks** - Confirm task tree and update plan
9. **complete** - Workflow finished

### Hybrid Approach

The skills and workflow are complementary:
- **`/gobby plan`**: Interactive flexibility for requirements and drafting
- **`/gobby expand`**: Creates tasks with TDD and validation
- **Workflow**: Deterministic expansion with enforced gates

Recommended pattern:
1. Use `/gobby plan` for Steps 1-5 (requirements through approval)
2. Use `/gobby expand <plan-file>` to create tasks
3. Or activate `plan-expansion` workflow for stricter enforcement
