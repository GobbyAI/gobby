# Plan: Reviewed Expansion Pipeline (`spec-to-tasks`)

## Context

Gobby's current `expand-task` pipeline is a single-shot process: one expander agent researches the codebase, produces a spec, pipeline validates and executes. No pre-gathered context, no review gates, no multi-level decomposition.

Inspired by BMAD-METHOD's phased agent pipeline (analyst → PM → architect → scrum master → developer), we're building a new **reviewed expansion pipeline** that validates specs through multiple specialized review stages before decomposition. The key insight: we don't need different agents — we need the same agent wearing different hats (different prompts, potentially different models) at each stage.

This is a **new pipeline** (`spec-to-tasks`), separate from `expand-task`. The existing pipeline stays as-is for quick expansions.

## Pipeline Architecture

```
Input: spec/plan + task_id + skip_review flag
                    │
          ┌─────────▼──────────┐
          │  gather_context     │  MCP tool (not agent)
          │  (codebase context) │
          └─────────┬──────────┘
                    │
        ┌───── skip_review? ─────┐
        │ true                   │ false
        │                ┌───────▼───────────┐
        │                │ codebase_reviewer  │  Agent (cheap model)
        │                │ "Is this feasible?"│
        │                └───────┬───────────┘
        │                ┌───────▼───────────┐
        │                │ architect_reviewer │  Agent (strong model)
        │                │ "Arch conformity?" │
        │                └───────┬───────────┘
        │                ┌───────▼───────────┐
        │                │ test_reviewer      │  Agent (mid model)
        │                │ "Testing strategy?"│
        │                └───────┬───────────┘
        │                ┌───────▼───────────┐
        │                │ final_gate         │  Agent (strong model)
        │                │ Pass → continue    │
        │                │ Fail → escalate    │
        │                └───────┬───────────┘
        │                        │
        └────────┬───────────────┘
                 │
        ┌────────▼────────────┐
        │  epic_expander      │  Agent
        │  Root epic +        │
        │  child epics +      │
        │  proposed task tree  │
        └────────┬────────────┘
                 │
        ┌────────▼────────────┐
        │  task_expander      │  dispatch_batch (parallel)
        │  Per child epic:    │
        │  RED → GREEN tasks  │
        │  (TDD structure)    │
        └─────────────────────┘
```

## Pipeline Input Variables

```yaml
inputs:
  task_id:
    type: string
    required: true
    description: "Task with spec/plan in description"
  session_id:
    type: string
    required: true
  skip_review:
    type: boolean
    default: false
    description: "Skip review stages, go straight to expansion"
  provider:
    default: "claude"
  codebase_model:
    default: null
    description: "Model for codebase researcher (cheap model recommended)"
  architect_model:
    default: null
    description: "Model for architect review (strong model recommended)"
  test_model:
    default: null
    description: "Model for test review"
  gate_model:
    default: null
    description: "Model for final gate decision"
  expander_model:
    default: null
    description: "Model for epic + task expansion"
```

## Implementation: 6 Deliverables

### 1. New MCP Tool: `gather_expansion_context`

**File**: `src/gobby/mcp_proxy/tools/tasks/_expansion.py`

New tool registered in `create_expansion_registry()`. Gathers structured context from multiple sources and returns formatted markdown.

```python
async def gather_expansion_context(
    task_id: str,
    project: str | None = None,
) -> dict[str, Any]:
    """Gather codebase context for expansion reviewers and agents."""
```

**Sources gathered** (in priority order, total budget ~30KB):

| Source | How | Priority |
|--------|-----|----------|
| Task details | `task_manager.get_task()` — full description, parent chain | 1 (always) |
| Code outlines | Parse file paths from description, use code index `get_file_outline()` | 2 |
| Sibling learnings | Children of same parent where `status='closed'` — `closed_reason`, `validation_feedback` | 3 |
| Parent context | If parent_task_id, get parent description + expansion_context | 4 |
| Relevant memories | `memory_manager.search()` with title keywords | 5 |

**Returns**: `{"context": str, "sources": ["task", "code_outlines", ...], "token_estimate": int}`

**Dependencies needed** (already available in RegistryContext or importable):
- `ctx.task_manager` — task queries
- Code index: import from `src/gobby/mcp_proxy/tools/code/_query.py` or call via internal registry
- Memory: import from `src/gobby/mcp_proxy/tools/memory.py` or call via internal registry

### 2. New Agent Definitions (4 reviewers + 2 expanders)

**Directory**: `src/gobby/install/shared/workflows/agents/`

Each agent is a YAML template with persona-specific instructions, step workflow, and optional default model.

#### `codebase-reviewer.yaml`
- **Job**: Read gathered context + spec. Assess whether the spec is feasible given the current codebase. Flag missing dependencies, nonexistent files/modules, API mismatches.
- **Output**: Calls `save_review_result` with `{pass: bool, concerns: [...], suggestions: [...]}`
- **Steps**: `review` → `terminate`
- **Blocked tools**: create_task, execute_expansion, spawn_agent

#### `architect-reviewer.yaml`
- **Job**: Read gathered context + spec + codebase review output. Assess architectural conformity: does the plan follow project patterns, respect module boundaries, handle cross-cutting concerns?
- **Output**: Calls `save_review_result` with `{pass: bool, concerns: [...], suggestions: [...]}`
- **Steps**: `review` → `terminate`

#### `test-reviewer.yaml`
- **Job**: Read gathered context + spec + prior reviews. Assess testing strategy: are test categories appropriate, is coverage adequate, are integration boundaries identified?
- **Output**: Calls `save_review_result` with `{pass: bool, concerns: [...], suggestions: [...]}`
- **Steps**: `review` → `terminate`

#### `expansion-gate.yaml`
- **Job**: Synthesize all review outputs. If all pass with no unresolved concerns → pass. If any critical concerns remain → fail with escalation summary.
- **Output**: Calls `save_review_result` with `{pass: bool, escalation_summary: str}`
- **Steps**: `evaluate` → `terminate`

#### `expansion-reconciler.yaml`
- **Job**: Read the original spec and the full created task tree (root epic → child epics → atomic tasks). Compare task titles, descriptions, and dependencies against spec requirements. Auto-fix: update task descriptions, add missing dependencies, correct dependency ordering. If a spec requirement has no corresponding task or a task is fundamentally wrong, escalate to user.
- **Output**: Calls `save_reconciliation_result` with `{passed: bool, fixes_applied: [...], escalations: [...]}`
- **Tools available**: `get_task`, `list_tasks`, `update_task`, `add_dependency`, `remove_dependency` + read-only codebase tools
- **Steps**: `reconcile` → `terminate`

#### `epic-expander.yaml`
- **Job**: Read spec + gathered context + review feedback. Create a root epic and child epics. Produce a tree view of proposed task structure with dependencies for the user to see.
- **Output**: Calls `save_expansion_spec` with epic-level spec (children are epics, not atomic tasks)
- **Steps**: `research` → `terminate`

#### `task-expander.yaml` (enhanced from existing `expander.yaml`)
- **Job**: Expand a single child epic into atomic tasks. For code epics, apply TDD structure:
  - **RED phase**: One test task covering all tests for this epic
  - **GREEN phase**: Atomic implementation tasks (each depends on RED)
  - Blue phase (QA) handled by orchestrator
- **Output**: Calls `save_expansion_spec` with atomic task spec
- **Steps**: `research` → `terminate`

### 3. New MCP Tool: `save_review_result`

**File**: `src/gobby/mcp_proxy/tools/tasks/_expansion.py`

Reviewers need somewhere to persist their output that the pipeline can read.

```python
async def save_review_result(
    task_id: str,
    reviewer: str,        # "codebase", "architect", "test", "gate"
    result: dict[str, Any],  # {pass: bool, concerns: [...], suggestions: [...]}
    project: str | None = None,
) -> dict[str, Any]:
```

**Storage**: Append to task's `expansion_context` JSON under a `reviews` key:
```json
{
  "reviews": {
    "codebase": {"pass": true, "concerns": [], "suggestions": ["..."]},
    "architect": {"pass": false, "concerns": ["X module doesn't exist"], ...},
    ...
  },
  "subtasks": [...]  // Populated later by expander
}
```

This keeps everything on the task record — survives compaction, accessible to downstream steps.

### 4. New Pipeline: `spec-to-tasks.yaml`

**File**: `src/gobby/install/shared/workflows/pipelines/spec-to-tasks.yaml`

```yaml
name: spec-to-tasks
type: pipeline
version: "1.0"
description: |
  Reviewed expansion pipeline. Validates specs through codebase, architecture,
  and testing review gates before multi-level decomposition into epics and tasks.

inputs:
  task_id: { type: string, required: true }
  session_id: { type: string, required: true }
  skip_review: { type: boolean, default: false }
  provider: "claude"
  codebase_model: null
  architect_model: null
  test_model: null
  gate_model: null
  expander_model: null

steps:
  # --- 1. Gather codebase context ---
  - id: gather_context
    mcp:
      server: gobby-tasks
      tool: gather_expansion_context
      arguments:
        task_id: "${{ inputs.task_id }}"

  # --- 2. Codebase review (skippable) ---
  - id: spawn_codebase_reviewer
    condition: "${{ not inputs.skip_review }}"
    mcp:
      server: gobby-agents
      tool: spawn_agent
      arguments:
        agent: codebase-reviewer
        task_id: "${{ inputs.task_id }}"
        model: "${{ inputs.codebase_model }}"
        provider: "${{ inputs.provider }}"
        parent_session_id: "${{ inputs.session_id }}"
        prompt: |
          Review this spec against the current codebase.

          ## Codebase Context
          ${{ steps.gather_context.output.context }}
        mode: interactive

  - id: wait_codebase
    condition: "${{ not inputs.skip_review }}"
    wait:
      completion_id: "${{ steps.spawn_codebase_reviewer.output.run_id }}"
      timeout: 300

  # --- 3. Architect review (skippable) ---
  - id: spawn_architect_reviewer
    condition: "${{ not inputs.skip_review }}"
    mcp:
      server: gobby-agents
      tool: spawn_agent
      arguments:
        agent: architect-reviewer
        task_id: "${{ inputs.task_id }}"
        model: "${{ inputs.architect_model }}"
        provider: "${{ inputs.provider }}"
        parent_session_id: "${{ inputs.session_id }}"
        prompt: |
          Review this spec for architectural conformity.

          ## Codebase Context
          ${{ steps.gather_context.output.context }}
        mode: interactive

  - id: wait_architect
    condition: "${{ not inputs.skip_review }}"
    wait:
      completion_id: "${{ steps.spawn_architect_reviewer.output.run_id }}"
      timeout: 300

  # --- 4. Test review (skippable) ---
  - id: spawn_test_reviewer
    condition: "${{ not inputs.skip_review }}"
    mcp:
      server: gobby-agents
      tool: spawn_agent
      arguments:
        agent: test-reviewer
        task_id: "${{ inputs.task_id }}"
        model: "${{ inputs.test_model }}"
        provider: "${{ inputs.provider }}"
        parent_session_id: "${{ inputs.session_id }}"
        prompt: |
          Review this spec's testing strategy.

          ## Codebase Context
          ${{ steps.gather_context.output.context }}
        mode: interactive

  - id: wait_test
    condition: "${{ not inputs.skip_review }}"
    wait:
      completion_id: "${{ steps.spawn_test_reviewer.output.run_id }}"
      timeout: 300

  # --- 5. Final gate (skippable) ---
  - id: spawn_gate
    condition: "${{ not inputs.skip_review }}"
    mcp:
      server: gobby-agents
      tool: spawn_agent
      arguments:
        agent: expansion-gate
        task_id: "${{ inputs.task_id }}"
        model: "${{ inputs.gate_model }}"
        provider: "${{ inputs.provider }}"
        parent_session_id: "${{ inputs.session_id }}"
        prompt: |
          Evaluate all review results and decide: pass or escalate.

          ## Codebase Context
          ${{ steps.gather_context.output.context }}
        mode: interactive

  - id: wait_gate
    condition: "${{ not inputs.skip_review }}"
    wait:
      completion_id: "${{ steps.spawn_gate.output.run_id }}"
      timeout: 300

  # --- 6. Gate check: escalate on failure ---
  - id: check_gate
    condition: "${{ not inputs.skip_review }}"
    mcp:
      server: gobby-tasks
      tool: check_review_gate
      arguments:
        task_id: "${{ inputs.task_id }}"
        # Returns {passed: bool}. If false, escalates task and fails pipeline.

  - id: gate_fail
    condition: "${{ not inputs.skip_review and steps.check_gate.output and not steps.check_gate.output.passed }}"
    mcp:
      server: gobby-workflows
      tool: fail_pipeline
      arguments:
        message: "Spec failed review. Task escalated to user."

  # --- 7. Epic expansion ---
  - id: spawn_epic_expander
    mcp:
      server: gobby-agents
      tool: spawn_agent
      arguments:
        agent: epic-expander
        task_id: "${{ inputs.task_id }}"
        model: "${{ inputs.expander_model }}"
        provider: "${{ inputs.provider }}"
        parent_session_id: "${{ inputs.session_id }}"
        prompt: |
          Create root epic and child epics from this spec.
          Produce a tree view with proposed task structure and dependencies.

          ## Context
          ${{ steps.gather_context.output.context }}
        mode: interactive

  - id: wait_epic_expander
    wait:
      completion_id: "${{ steps.spawn_epic_expander.output.run_id }}"
      timeout: 600

  # --- 8. Validate + execute epic expansion ---
  - id: validate_epics
    mcp:
      server: gobby-tasks
      tool: validate_expansion_spec
      arguments:
        task_id: "${{ inputs.task_id }}"

  - id: check_epics_valid
    condition: "${{ not steps.validate_epics.output.valid }}"
    mcp:
      server: gobby-workflows
      tool: fail_pipeline
      arguments:
        message: "Epic expansion spec invalid: ${{ steps.validate_epics.output.errors }}"

  - id: execute_epics
    mcp:
      server: gobby-tasks
      tool: execute_expansion
      arguments:
        parent_task_id: "${{ inputs.task_id }}"
        session_id: "${{ inputs.session_id }}"

  # --- 9. Expand child epics into atomic tasks (parallel via dispatch) ---
  - id: expand_children
    condition: "${{ steps.execute_epics.output.count > 0 }}"
    mcp:
      server: gobby-tasks
      tool: expand_child_epics
      arguments:
        parent_task_id: "${{ inputs.task_id }}"
        child_refs: "${{ steps.execute_epics.output.created }}"
        session_id: "${{ inputs.session_id }}"
        provider: "${{ inputs.provider }}"
        model: "${{ inputs.expander_model }}"

  # --- 10. Reconciliation: verify tasks match spec ---
  - id: spawn_reconciler
    condition: "${{ steps.expand_children.output.total_created > 0 }}"
    mcp:
      server: gobby-agents
      tool: spawn_agent
      arguments:
        agent: expansion-reconciler
        task_id: "${{ inputs.task_id }}"
        model: "${{ inputs.gate_model }}"
        provider: "${{ inputs.provider }}"
        parent_session_id: "${{ inputs.session_id }}"
        prompt: |
          Compare the created task tree against the original spec.
          Fix any dependency errors, bad descriptions, or missing coverage.
          Escalate to user if anything can't be auto-fixed.

          ## Original Spec Context
          ${{ steps.gather_context.output.context }}
        mode: interactive

  - id: wait_reconciler
    wait:
      completion_id: "${{ steps.spawn_reconciler.output.run_id }}"
      timeout: 600

  # --- 11. Check reconciliation result ---
  - id: check_reconciliation
    mcp:
      server: gobby-tasks
      tool: check_reconciliation_result
      arguments:
        task_id: "${{ inputs.task_id }}"

  - id: reconciliation_fail
    condition: "${{ steps.check_reconciliation.output and not steps.check_reconciliation.output.passed }}"
    mcp:
      server: gobby-workflows
      tool: fail_pipeline
      arguments:
        message: "Reconciliation failed. Task escalated: ${{ steps.check_reconciliation.output.reason }}"
```

### 5. New MCP Tool: `expand_child_epics`

**File**: `src/gobby/mcp_proxy/tools/tasks/_expansion.py`

Since pipelines don't have native foreach, this tool handles the iteration internally.

```python
async def expand_child_epics(
    parent_task_id: str,
    child_refs: list[str],
    session_id: str,
    provider: str = "claude",
    model: str | None = None,
    project: str | None = None,
) -> dict[str, Any]:
    """Expand each child epic by invoking expand-task sub-pipeline per child."""
```

For each child ref:
1. Invoke `expand-task` pipeline with `task_id=child_ref`
2. Run in parallel via `asyncio.gather()`
3. Collect results

The existing `expand-task` pipeline handles the actual expansion. The task-expander agent (enhanced) applies TDD structure for code epics.

### 6. New MCP Tools: `save_reconciliation_result` + `check_reconciliation_result`

**File**: `src/gobby/mcp_proxy/tools/tasks/_expansion.py`

```python
async def save_reconciliation_result(
    task_id: str,
    result: dict[str, Any],  # {passed: bool, fixes_applied: [...], escalations: [...]}
    project: str | None = None,
) -> dict[str, Any]:
    """Save reconciliation result to task's expansion_context."""
```

Appends to `expansion_context.reconciliation`:
```json
{
  "reconciliation": {
    "passed": true,
    "fixes_applied": [
      {"task_ref": "#45", "action": "added_dependency", "detail": "#45 now depends on #43"}
    ],
    "escalations": []
  }
}
```

```python
async def check_reconciliation_result(
    task_id: str,
    project: str | None = None,
) -> dict[str, Any]:
    """Check reconciliation result. If escalations exist, escalate task."""
```

If escalations non-empty:
- Set task status to `escalated`
- Return `{"passed": false, "reason": "N issues require user attention", "escalations": [...]}`

The reconciler agent has write access to tasks (`update_task`, `add_dependency`, `remove_dependency`) so it can fix issues directly. Only issues it can't fix get escalated.

### 7. New MCP Tool: `check_review_gate`

**File**: `src/gobby/mcp_proxy/tools/tasks/_expansion.py`

Reads review results from `expansion_context.reviews`, checks if gate reviewer passed.

```python
async def check_review_gate(
    task_id: str,
    project: str | None = None,
) -> dict[str, Any]:
    """Check if the expansion gate review passed. If failed, escalate task."""
```

If gate result is `pass: false`:
- Set task status to `escalated`
- Return `{"passed": false, "reason": "..."}`

## TDD Task Structure (per code child epic)

When the task-expander processes a child epic with `category: code`:

```
Child Epic: "Add user authentication"
├── [RED]  Write tests for user authentication    (category: test)
├── [GREEN] Implement login endpoint              (category: code, depends_on: [RED])
├── [GREEN] Implement session management          (category: code, depends_on: [RED])
├── [GREEN] Implement logout endpoint             (category: code, depends_on: [RED])
└── (Blue/QA phase handled by orchestrator pipeline)
```

The task-expander agent's instructions enforce this structure:
- First subtask is always the RED phase (all tests for the epic)
- GREEN subtasks each depend on the RED task
- No explicit REFACTOR/blue task — orchestrator QA handles that

## Files to Create

| File | What |
|------|------|
| `src/gobby/install/shared/workflows/pipelines/spec-to-tasks.yaml` | New pipeline |
| `src/gobby/install/shared/workflows/agents/codebase-reviewer.yaml` | Codebase review agent |
| `src/gobby/install/shared/workflows/agents/architect-reviewer.yaml` | Architect review agent |
| `src/gobby/install/shared/workflows/agents/test-reviewer.yaml` | Test review agent |
| `src/gobby/install/shared/workflows/agents/expansion-gate.yaml` | Gate agent |
| `src/gobby/install/shared/workflows/agents/epic-expander.yaml` | Epic-level expander |
| `src/gobby/install/shared/workflows/agents/expansion-reconciler.yaml` | Reconciliation agent |

## Files to Modify

| File | Change |
|------|--------|
| `src/gobby/mcp_proxy/tools/tasks/_expansion.py` | Add `gather_expansion_context`, `save_review_result`, `check_review_gate`, `expand_child_epics`, `save_reconciliation_result`, `check_reconciliation_result` |
| `src/gobby/install/shared/workflows/agents/expander.yaml` | Enhance instructions for TDD structure (becomes `task-expander` behavior) |
| `src/gobby/tasks/prompts/expand-task.md` | Add multi-perspective analysis section |

## Existing Code to Reuse

| What | Where | How |
|------|-------|-----|
| `dispatch_batch` pattern | `src/gobby/mcp_proxy/tools/spawn_agent/_factory.py:313-394` | Reference for parallel agent spawning |
| `invoke_pipeline` | `src/gobby/workflows/pipeline_executor.py:585-589` | Sub-pipeline invocation for child epic expansion |
| `ContextResolver` | `src/gobby/agents/context.py` | Context injection patterns |
| `save_expansion_spec` | `src/gobby/mcp_proxy/tools/tasks/_expansion.py` | Reuse for both epic + task level specs |
| `validate_expansion_spec` | Same file | Reuse at both expansion levels |
| `execute_expansion` | Same file | Reuse at both expansion levels |
| Agent step workflow pattern | `src/gobby/install/shared/workflows/agents/expander.yaml` | Template for new reviewer agents |

## Verification

1. **Unit tests** for new MCP tools:
   - `gather_expansion_context`: test each source (task details, code outlines, siblings, memories, parent)
   - `save_review_result`: test append to expansion_context.reviews
   - `check_review_gate`: test pass/fail/escalation
   - `expand_child_epics`: test parallel expansion of N children

2. **Agent definition tests**: validate YAML structure, step workflow transitions, blocked tools

3. **Pipeline integration test**: run `spec-to-tasks` with `skip_review=true` to test expansion path without reviewers

4. **Pipeline integration test**: run with `skip_review=false` using mock agents to test review → gate → expansion flow

5. **TDD structure test**: expand a code epic, verify RED task created first, GREEN tasks depend on it

6. **Reconciliation tests**:
   - `save_reconciliation_result`: test persist to expansion_context.reconciliation
   - `check_reconciliation_result`: test pass (no escalations), fail (escalations → task escalated)
   - Reconciler agent fixes a bad dependency → verify task dependency updated
   - Reconciler agent can't fix a missing spec requirement → escalates

7. **Manual test**: `run_pipeline spec-to-tasks` with a real spec, verify full flow end-to-end

## Build Order

1. `gather_expansion_context` tool (needed by everything)
2. `save_review_result` + `check_review_gate` tools
3. `save_reconciliation_result` + `check_reconciliation_result` tools
4. Reviewer agent definitions (4 YAML files)
5. Epic-expander agent definition
6. Expansion-reconciler agent definition
7. Enhanced expander.yaml (TDD structure)
8. `expand_child_epics` tool
9. `spec-to-tasks.yaml` pipeline (ties everything together)
10. Updated `expand-task.md` prompt
11. Tests at each step
