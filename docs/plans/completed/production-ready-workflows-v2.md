# Unified Workflow Architecture

## Context

Gobby currently has three separate execution models that share infrastructure but can't compose:

| Model | Execution | Activation | Composability |
|-------|-----------|------------|---------------|
| **Lifecycle workflow** | Event-driven triggers | Always on | None |
| **Step workflow** | State machine | Manual `activate_workflow` | None |
| **Pipeline** | Sequential/deterministic | Manual `run_pipeline` | `invoke_pipeline` only |

This creates friction:
- Lifecycle and step workflows use different code paths in `engine.py` despite sharing variables, conditions, and actions
- Step workflows are artificially limited to one-per-session
- Pipelines can't invoke workflows; workflows can't invoke pipelines
- The "type" distinction (lifecycle vs step) confuses users — they're the same concept with different capabilities enabled

**Goal:** Unify lifecycle + step into a single workflow type with optional capabilities, and enable bidirectional composability between workflows and pipelines.

---

## Part 1: Unified Workflow Type

### Design

Every workflow is a single type. No `type` field. Capabilities are determined by what's declared:

```yaml
name: my-workflow
priority: 20           # Execution ordering (lower = first)
enabled: true          # true = always-on, false = activate via set_variable
sources: [claude]      # Optional CLI filter

variables: { ... }          # Workflow-scoped variables
session_variables: { ... }  # Session-scoped shared variables
observers: [ ... ]          # Event watchers that set variables

# Reactive capabilities (event-driven)
triggers:
  on_before_tool: [ ... ]
  on_stop: [ ... ]

# Proactive capabilities (state machine)
steps:
  - name: work
    on_enter: [ ... ]
    transitions: [ ... ]

# Tool enforcement
tool_rules: [ ... ]

# Step extras (only meaningful when steps exist)
exit_condition: "..."
on_premature_stop: { ... }
```

### Key rules

1. **`enabled: true`** (default) = workflow evaluates on every hook event.
2. **`enabled: false`** = workflow is dormant. Activate by calling `set_variable(name="enabled", value=true, workflow="my-workflow")`.
3. **Triggers are always evaluated** when enabled, regardless of whether steps exist or which step is current.
4. **Steps are optional.** A triggers-only workflow is valid. A steps-only workflow is valid. Both together is the new capability.
5. **Multiple workflows active simultaneously.** No one-step-workflow limit. Priority ordering resolves conflicts. First block wins.
6. **Steps can reference triggers.** Trigger conditions can include `variables._current_step == "red"` to scope behavior to a step.

### Concrete examples

**session-lifecycle (triggers only, always on):**
```yaml
name: session-lifecycle
priority: 10
enabled: true
session_variables:
  unlocked_tools: []
  task_claimed: false
  servers_listed: false
  plan_mode: false
observers:
  - name: task_tracking
    behavior: task_claim_tracking
triggers:
  on_session_start:
    - action: inject_context
      filter: context_aware
  on_before_tool:
    - when: "not session.task_claimed"
      action: block_tools
      rules: [{ tool: Edit, decision: block }]
```

**auto-task (triggers only, activatable):**
```yaml
name: auto-task
priority: 25
enabled: false
variables:
  assigned_task_id: null
  context_injected: false
triggers:
  on_before_agent:
    - when: "not variables.context_injected"
      action: inject_message
      content: "Autonomous mode. Task: {{ variables.assigned_task_id }}"
    - when: "not variables.context_injected"
      action: set_variable
      name: context_injected
      value: true
  on_stop:
    - when: "not task_tree_complete(variables.assigned_task_id)"
      action: block
      message: "Task incomplete. Keep working."
```

**developer (steps + triggers, activatable):**
```yaml
name: developer
priority: 20
enabled: false
variables:
  assigned_task_id: null
  tests_written: false
  tests_passing: false

steps:
  - name: red
    on_enter:
      - action: inject_message
        content: "Write failing tests first."
    allowed_tools: [Read, Grep, Glob, Write, Edit]
    rules:
      - name: test_files_only
        when: "not is_test_file(tool_input.file_path)"
        tool: [Write, Edit]
        decision: block
    transitions:
      - to: green
        when: "variables.tests_written"

  - name: green
    on_enter:
      - action: inject_message
        content: "Make the tests pass."
    transitions:
      - to: blue
        when: "variables.tests_passing"

  # ... more steps

triggers:
  on_stop:
    - when: "not variables.exit_condition_met"
      action: block
      message: "Finish your TDD cycle."

exit_condition: "variables.task_complete"
```

### Activation API

```python
# Activate — convenience tool that sets enabled=true + merges variables
activate_workflow(name="developer", session_id="#123", variables={"assigned_task_id": "#456"})

# Deactivate — sets enabled=false + clears step state + clears workflow variables
end_workflow(workflow="developer", session_id="#123")

# Direct variable control
set_variable(name="enabled", value=True, workflow="developer", session_id="#123")
```

### Engine: single evaluation loop

```
# Build index once on workflow load/change (not per-event)
workflows_by_trigger: dict[str, list[Workflow]]  # event_type → sorted workflows

# Per-event evaluation
for each workflow in workflows_by_trigger[event_type]:
    if not workflow.enabled: skip
    evaluate triggers (event-driven) → accumulate context/messages
    if workflow.has_steps:
        evaluate step state (tool restrictions, transitions, on_enter/on_exit)
    if decision == block: stop evaluation, return block
    # Otherwise continue to next workflow (accumulate context)
```

**Accumulate context, first block stops.** All enabled workflows get a chance to inject context (messages, variables). But the first workflow that returns a `block` decision halts evaluation — no further workflows run for that event. This means a priority-10 "security" workflow can block before a priority-20 "logger" workflow runs, but a priority-10 "context injector" won't prevent a priority-20 "enforcer" from blocking.

**Performance:** Workflows are indexed by trigger type at load time. On a `before_tool` event, only workflows with `on_before_tool` triggers (or steps with tool rules) are evaluated — not all enabled workflows. This prevents latency on high-frequency events.

Triggers and steps are layers, not alternatives. Triggers fire on events. Steps provide structure and tool restrictions. A workflow can use either or both.

### State model

- One state entry per workflow, keyed by workflow name
- `step` field is null when workflow has no steps, or the current step name
- `enabled` field on the state
- Workflow variables stored per-workflow

---

## Part 2: Variable Scoping

### Two namespaces

**Workflow variables** — isolated per-workflow:
```yaml
variables:
  tests_written: false      # only this workflow reads/writes
  tests_passing: false
```

**Session variables** — shared namespace visible to all workflows:
```yaml
session_variables:
  unlocked_tools: []        # any workflow can read/write
  task_claimed: false
```

### Access

| Operation | Syntax | Scope |
|-----------|--------|-------|
| Read own variable | `variables.tests_written` | Workflow |
| Write own variable | `set_variable(name, value, workflow)` | Workflow |
| Read session variable | `session.task_claimed` | Session (shared) |
| Write session variable | `set_session_variable(name, value)` | Session (shared) |
| Read another workflow's variable | Not allowed | — |

### MCP tools

```python
# Workflow-scoped
set_variable(name="tests_written", value=True, workflow="developer", session_id="#123")
get_variable(name="tests_written", workflow="developer", session_id="#123")

# Session-scoped
set_session_variable(name="task_claimed", value=True, session_id="#123")
get_session_variable(name="task_claimed", session_id="#123")
```

### Lifecycle & isolation

- `session_variables` declared in any workflow YAML. First workflow to declare a session variable sets its default.
- Session variables persist for the entire session lifetime, regardless of which workflows are enabled/disabled.
- Workflow variables are created when enabled, cleared when disabled.
- **Storage isolation:** Workflow variables and session variables are stored in separate dicts in the state model — not merged into a single namespace. The `variables.*` and `session.*` prefixes in condition expressions resolve to different backing stores. Writing to `variables.foo` can never leak into `session.foo`.

### Variable name collision resolution

When the deprecated `variables.<name>` form is used (without explicit `variables.*` or `session.*` prefix), the system resolves by checking workflow-scoped variables first, then session-scoped variables. A deprecation warning is emitted advising callers to use the explicit prefix:

- **MCP APIs:** `set_variable`/`get_variable` default to workflow-scoped. Use `set_session_variable`/`get_session_variable` for session-scoped access.
- **Condition expressions:** Bare `variables.get('key')` checks workflow scope first, then session scope, with a deprecation warning logged. Prefer `variables.key` (workflow) or `session.key` (session).

### Condition context

Both namespaces available in `when` expressions and templates:
```yaml
triggers:
  on_before_tool:
    - when: "not session.task_claimed"
      action: block_tools

steps:
  - name: red
    transitions:
      - to: green
        when: "variables.tests_written"
```

---

## Part 3: Pipeline/Workflow Composability

### Pipeline inside workflow

New action type `run_pipeline` available in triggers and step on_enter/on_exit:

```yaml
steps:
  - name: validate
    on_enter:
      - action: run_pipeline
        pipeline: ci-checks
        inputs:
          task_id: "{{ variables.assigned_task_id }}"
          commit_sha: "{{ variables.last_commit }}"
        result_variable: validation
    transitions:
      - to: commit
        when: "variables.validation.all_passed"
      - to: fix
        when: "not variables.validation.all_passed"
```

**Semantics:**
- Pipeline executes synchronously within the workflow action
- Pipeline outputs stored in the specified workflow variable
- If pipeline hits an approval gate, the workflow action pauses (hook returns, agent continues, pipeline resumes when approved)
- Pipeline failure sets `result_variable` to `{error: "...", failed: true}`

### Workflow inside pipeline (orchestrator pattern)

New pipeline step type `spawn_session`:

```yaml
steps:
  - id: implement
    spawn_session:
      agent: claude
      workflow: developer
      variables:
        assigned_task_id: "${{ steps.find_task.output.task_id }}"
      wait_for: exit_condition
      timeout: 3600
    # output = workflow's final variables

  - id: review
    prompt: "Review changes from ${{ steps.implement.output.last_commit }}"
```

**Semantics:**
- Pipeline executor spawns a new terminal/session via existing spawner infrastructure
- Activates the specified workflow on that session
- If `wait_for: exit_condition`, pipeline blocks until workflow signals completion
- Workflow's final variables become the step's output
- Timeout prevents infinite waits
- Requires tmux (uses consolidated `TmuxSpawner`). On Windows, requires WSL with tmux installed. Pipeline parsing should validate tmux availability early, failing with an actionable error if tmux is not found.

### Workflow inside pipeline (inline pattern)

New pipeline step type `activate_workflow`:

```yaml
steps:
  - id: setup
    activate_workflow:
      name: auto-task
      variables:
        assigned_task_id: "${{ steps.find_task.output.task_id }}"
    # Non-blocking — pipeline continues, workflow runs alongside

  - id: wait_for_completion
    activate_workflow:
      name: auto-task
      wait_for: exit_condition
      timeout: 1800
    # Blocking — pipeline waits for workflow to finish
```

**Semantics:**
- Activates workflow on the pipeline's session (sets `enabled=true` + variables)
- Non-blocking by default, optional `wait_for` to block
- Useful for "configure the agent's behavior then let it work"

### Pipeline inside pipeline (existing)

Already works via `invoke_pipeline`. No changes needed.

---

## Part 4: Directory Structure

```
.gobby/workflows/
  session-lifecycle.yaml    # All workflows in one flat directory
  developer.yaml
  auto-task.yaml
  ci-pipeline.yaml          # Pipelines coexist (type: pipeline)
```

No `lifecycle/` subdirectory. No `type` field on workflows. Pipelines keep `type: pipeline` — they're a fundamentally different execution model (deterministic/sequential vs reactive/event-driven). Composability bridges them without merging them.

---

## Summary

| Concept | Design |
|---------|--------|
| **Workflow type** | One unified type. `triggers:` for reactive, `steps:` for state machine, both optional. |
| **Activation** | `enabled: true/false`. `activate_workflow` / `end_workflow` as convenience sugar. |
| **Concurrency** | Multiple workflows active simultaneously. Priority ordering. First block wins. |
| **Workflow variables** | Isolated per-workflow via `variables:`. Accessed as `variables.*`. |
| **Session variables** | Shared namespace via `session_variables:`. Accessed as `session.*`. |
| **Pipeline → Workflow** | `run_pipeline` action in triggers/steps. Synchronous execution. |
| **Workflow → Pipeline (orchestrator)** | `spawn_session` pipeline step. Spawns agent with workflow, waits for completion. |
| **Workflow → Pipeline (inline)** | `activate_workflow` pipeline step. Activates on current session. |
| **Pipeline type** | Unchanged. `type: pipeline` remains a distinct execution model. |

---

## Part 5: Implementation Phases

### Phase 1: State Model

**Goal:** Support multiple concurrent workflow instances per session with isolated variable storage.

**Current state:**
- `workflow_states` table: one row per `session_id` (primary key), single `variables` JSON blob
- `workflow_name` stores either the active step workflow name, `__lifecycle__`, or `__ended__`
- Lifecycle and step workflows share one variable namespace — no isolation
- `WorkflowStateManager.delete_state()` sets `workflow_name='__ended__'` but preserves variables

**Changes:**

1. **New `workflow_instances` table** (migration v99):

```sql
CREATE TABLE workflow_instances (
    id TEXT PRIMARY KEY,                   -- UUID
    session_id TEXT NOT NULL,
    workflow_name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,    -- 0=dormant, 1=active
    priority INTEGER NOT NULL DEFAULT 100,
    current_step TEXT,                      -- NULL for triggers-only workflows
    step_entered_at TEXT,
    step_action_count INTEGER DEFAULT 0,
    total_action_count INTEGER DEFAULT 0,
    variables TEXT DEFAULT '{}',            -- Workflow-scoped variables (JSON)
    context_injected INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(session_id, workflow_name),
    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX idx_workflow_instances_session ON workflow_instances(session_id);
CREATE INDEX idx_workflow_instances_enabled ON workflow_instances(session_id, enabled);
```

2. **New `session_variables` table** (migration v99):

```sql
CREATE TABLE session_variables (
    session_id TEXT PRIMARY KEY,
    variables TEXT DEFAULT '{}',            -- Session-scoped shared variables (JSON)
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

3. **Data migration** (v99 Python migration):
   - For each row in `workflow_states`:
     - Create a `session_variables` row with the existing `variables` JSON
     - If `workflow_name` not in (`__lifecycle__`, `__ended__`): create a `workflow_instances` row
   - Keep `workflow_states` table temporarily for rollback safety; drop in a later migration

4. **New `WorkflowInstanceManager`** (replaces per-workflow parts of `WorkflowStateManager`):
   - `get_instance(session_id, workflow_name) -> WorkflowInstance | None`
   - `get_active_instances(session_id) -> list[WorkflowInstance]`
   - `save_instance(instance)`
   - `delete_instance(session_id, workflow_name)`
   - `set_enabled(session_id, workflow_name, enabled: bool)`

5. **New `SessionVariableManager`** (replaces session-scoped parts of `WorkflowStateManager`):
   - `get_variables(session_id) -> dict`
   - `set_variable(session_id, name, value)`
   - `merge_variables(session_id, updates: dict)` (atomic read-modify-write)
   - `delete_variables(session_id)`

6. **Update `WorkflowState` model** in `definitions.py`:
   - Split into `WorkflowInstance` (per-workflow) and keep `WorkflowState` as a compatibility shim during migration
   - `WorkflowInstance` fields: `id`, `session_id`, `workflow_name`, `enabled`, `priority`, `current_step`, `step_entered_at`, `step_action_count`, `total_action_count`, `variables` (workflow-scoped only), `context_injected`

**Files:**

| File | Change |
|------|--------|
| `src/gobby/storage/migrations.py` | Add migration v99 (tables + data migration) |
| `src/gobby/workflows/state_manager.py` | Add `WorkflowInstanceManager`, `SessionVariableManager` |
| `src/gobby/workflows/definitions.py` | Add `WorkflowInstance` model |

---

### Phase 2: Definition Schema + Loader

**Goal:** Remove the lifecycle/step type distinction. Add `enabled`, `priority`, `session_variables` to schema. Flatten directory discovery.

**Current state:**
- `WorkflowDefinition.type: Literal["lifecycle", "step"]` — hardcoded distinction
- Lifecycle workflows discovered from `lifecycle/` subdirectories only
- Step workflows loaded by name (no discovery scan)
- `settings.priority` used for lifecycle ordering (default 100)
- Variables declared under `variables:` — no session vs workflow scoping

**Changes:**

1. **Update `WorkflowDefinition`** in `definitions.py:136`:
   - Deprecate `type` field — keep for backward compat, default to `None`, ignore in new code
   - Add `enabled: bool = True` (replaces `type: lifecycle` = always-on)
   - Add `priority: int = 100` (top-level, replaces `settings.priority`)
   - Add `session_variables: dict[str, Any]` (shared namespace declarations)
   - Keep `variables:` for workflow-scoped variables

```python
class WorkflowDefinition(BaseModel):
    name: str
    description: str | None = None
    version: str = "1.0"
    type: Literal["lifecycle", "step"] | None = None  # Deprecated, ignored
    enabled: bool = True          # true = always-on, false = activate via API
    priority: int = 100           # Lower = runs first
    session_variables: dict[str, Any] = Field(default_factory=dict)
    # ... rest unchanged
```

   **Deprecation handling:** When the loader encounters a YAML with a `type` field, it should emit a `DeprecationWarning` explaining the mapping (`type: lifecycle` → `enabled: true`, `type: step` → `enabled: false`) and instruct users to remove `type` and use `enabled` directly.

2. **Update `WorkflowLoader.discover_lifecycle_workflows()`** → rename to `discover_workflows()`:
   - Scan both root `workflows/` directory AND `lifecycle/` subdirectory (backward compat)
   - Return ALL workflow definitions (not just `type == "lifecycle"`)
   - Filter by `enabled` status in the engine, not the loader
   - Keep `discover_lifecycle_workflows()` as a deprecated alias

3. **Update `_scan_directory()`** in `loader.py:978`:
   - Remove the `type == "lifecycle"` filter at line 771
   - Use `definition.priority` directly instead of `settings.priority`

4. **Update `_find_workflow_file()`** in `loader.py:414`:
   - Also scan root directory for workflows previously only in `lifecycle/`

**Files:**

| File | Change |
|------|--------|
| `src/gobby/workflows/definitions.py` | Add `enabled`, `priority`, `session_variables` to `WorkflowDefinition` |
| `src/gobby/workflows/loader.py` | Unified discovery, flatten directory scanning |

---

### Phase 3: Unified Engine

**Goal:** Merge the separate step-workflow and lifecycle-workflow evaluation paths into one loop.

**Current state:**
- Step workflows: `WorkflowEngine.handle_event()` (`engine.py:142`) — single-workflow, full step/rule/transition logic
- Lifecycle workflows: `evaluate_all_lifecycle_workflows()` (`lifecycle_evaluator.py:514`) — multi-workflow discovery + trigger evaluation
- These run independently — lifecycle first (via `handle_all_lifecycles`), then step (via `handle` on the same engine)
- Lifecycle evaluator creates temporary `WorkflowState` objects per evaluation
- Step engine reads/writes persistent `WorkflowState` from `workflow_states` table

**Changes:**

1. **New `unified_evaluator.py`** — single evaluation function:

```python
async def evaluate_event(
    event: HookEvent,
    instance_manager: WorkflowInstanceManager,
    session_var_manager: SessionVariableManager,
    loader: WorkflowLoader,
    action_executor: ActionExecutor,
    evaluator: ConditionEvaluator,
    observer_engine: ObserverEngine,
) -> HookResponse:
    """
    Single entry point for all workflow evaluation.

    1. Discover all workflows for this session's project
    2. Filter to enabled workflows with triggers for this event type
    3. Sort by priority
    4. For each workflow:
       a. Load/create workflow instance
       b. Evaluate triggers (event-driven)
       c. If workflow has steps: evaluate step logic (tool restrictions, transitions)
       d. Accumulate context; first block wins
    5. Evaluate observers
    6. Persist state changes
    """
```

2. **Build trigger index at discovery time** (not per-event):
   - `WorkflowLoader` caches `workflows_by_trigger: dict[str, list[WorkflowDefinition]]`
   - On `before_tool` event, only workflows with `on_before_tool` triggers or step tool rules are evaluated

3. **Evaluation context** includes both namespaces:

```python
eval_context = {
    "variables": DotDict(instance.variables),    # Workflow-scoped
    "session": DotDict(session_variables),         # Session-scoped shared
    "event": event,
    "tool_name": ...,
    # Flatten workflow variables to top level for backward compat
    **instance.variables,
}
```

4. **Extract reusable logic from `engine.py`:**
   - `_evaluate_step_tool_rules()` — tool allow/block/MCP restriction logic (lines 301-411)
   - `_evaluate_step_transitions()` — transition checking + auto-chain (lines 443-476)
   - `_evaluate_triggers()` — trigger condition + action execution (from `lifecycle_evaluator.py:249-316`)

5. **Deprecation path:**
   - Keep `WorkflowEngine.handle_event()` and `evaluate_all_lifecycle_workflows()` as thin wrappers calling `evaluate_event()`
   - Remove wrappers in a follow-up release

**Files:**

| File | Change |
|------|--------|
| `src/gobby/workflows/unified_evaluator.py` | **New file** — single evaluation loop |
| `src/gobby/workflows/engine.py` | Delegate to `unified_evaluator.py`, keep as compatibility shim |
| `src/gobby/workflows/lifecycle_evaluator.py` | Delegate to `unified_evaluator.py`, keep as compatibility shim |

---

### Phase 4: Hook Integration

**Goal:** Consolidate scattered `handle_all_lifecycles()` call sites into a single dispatch path.

**Current state — 10 call sites across 4 files:**

| File | Line(s) | Event |
|------|---------|-------|
| `hooks/event_handlers/_agent.py` | 58 | `before_agent` |
| `hooks/event_handlers/_agent.py` | 205 | `after_agent` |
| `hooks/event_handlers/_agent.py` | 237 | `stop` |
| `hooks/event_handlers/_agent.py` | 277 | `stop` (fallback) |
| `hooks/event_handlers/_session.py` | 264 | `session_start` |
| `hooks/event_handlers/_session.py` | 328 | `session_end` |
| `hooks/event_handlers/_session.py` | 461 | `pre_compact` |
| `hooks/event_handlers/_tool.py` | 35 | `before_tool` |
| `hooks/event_handlers/_tool.py` | 104 | `after_tool` |
| `servers/websocket/chat.py` | 174 | WebSocket chat |

Each call site duplicates the same pattern:
```python
if self._workflow_handler:
    try:
        wf_response = self._workflow_handler.handle_all_lifecycles(event)
        if wf_response.context:
            context_parts.append(wf_response.context)
        if wf_response.decision != "allow":
            return wf_response
    except Exception as e:
        self.logger.error(...)
```

**Changes:**

1. **Update `WorkflowHookHandler`** in `hooks.py`:
   - Rename `handle_all_lifecycles()` → `evaluate()` (single unified method)
   - `handle_all_lifecycles()` becomes deprecated alias
   - Remove `handle()` (step-only method) — unified evaluator handles both
   - Remove `handle_lifecycle()` (single-workflow method) — unified evaluator handles all

2. **Consolidate call sites** — extract to `EventHandlersBase`:
   - Add `_evaluate_workflows(event) -> HookResponse` to `_base.py`
   - Each handler calls `self._evaluate_workflows(event)` instead of inline try/except
   - Reduces 10 call sites to 1 implementation + 10 one-liner calls

3. **WebSocket path** (`chat.py:174`):
   - Update to use the same unified handler

**Files:**

| File | Change |
|------|--------|
| `src/gobby/workflows/hooks.py` | Rename/simplify methods, delegate to unified evaluator |
| `src/gobby/hooks/event_handlers/_base.py` | Add `_evaluate_workflows()` helper |
| `src/gobby/hooks/event_handlers/_agent.py` | Replace 4 inline blocks with helper call |
| `src/gobby/hooks/event_handlers/_session.py` | Replace 3 inline blocks with helper call |
| `src/gobby/hooks/event_handlers/_tool.py` | Replace 2 inline blocks with helper call |
| `src/gobby/servers/websocket/chat.py` | Update to unified handler |

---

### Phase 5: MCP Tool Updates

**Goal:** Update MCP workflow tools for multi-workflow activation and scoped variables.

**Current state:**
- `activate_workflow()` in `_lifecycle.py` — blocks if any step workflow already active
- `set_variable()` in `_variables.py` — writes to single `variables` blob (no scoping)
- `get_variable()` in `_variables.py` — reads from single `variables` blob
- `end_workflow()` in `_lifecycle.py` — clears step state, preserves variables

**Changes:**

1. **Update `activate_workflow()`** in `_lifecycle.py`:
   - Remove single-step-workflow restriction
   - Create `workflow_instances` row with `enabled=True`
   - Merge `session_variables` declarations into `session_variables` table
   - Support activating workflows with `enabled: false` in YAML (dormant until activated)

2. **Update `end_workflow()`** in `_lifecycle.py`:
   - Accept `workflow` parameter to end a specific workflow (not just "the active one")
   - Set `enabled=False` on the instance, clear step state
   - Clear workflow-scoped variables; leave session variables untouched

3. **New `set_session_variable()`** tool:
   - Writes to `session_variables` table (shared namespace)
   - Accessible as `session.*` in condition expressions

4. **Update `set_variable()`** in `_variables.py`:
   - Add `workflow` parameter — writes to workflow-scoped variables
   - Without `workflow` parameter: backward-compat writes to session variables (with deprecation warning)

5. **Update `get_variable()`** in `_variables.py`:
   - Add `workflow` parameter — reads from workflow-scoped variables
   - Without `workflow` parameter: reads from session variables

6. **Update `_query.py`** — workflow status tool:
   - Return all active workflow instances (not just one)
   - Show per-workflow variables and session variables separately

**Files:**

| File | Change |
|------|--------|
| `src/gobby/mcp_proxy/tools/workflows/_lifecycle.py` | Multi-workflow activation, per-workflow end |
| `src/gobby/mcp_proxy/tools/workflows/_variables.py` | Scoped variables, new `set_session_variable` |
| `src/gobby/mcp_proxy/tools/workflows/_query.py` | Multi-workflow status reporting |
| `src/gobby/mcp_proxy/tools/workflows/__init__.py` | Register new tools |

---

### Phase 6: Pipeline Composability

**Goal:** Enable bidirectional workflow/pipeline composition.

**Current state:**
- `PipelineExecutor` in `pipeline_executor.py` — handles exec, prompt, invoke_pipeline, mcp step types
- `PipelineStep` model requires exactly one of: `exec`, `prompt`, `invoke_pipeline`, `mcp`
- No `run_pipeline` action in workflow actions
- No `spawn_session` or `activate_workflow` pipeline step types

**Changes:**

1. **New `run_pipeline` workflow action** in `actions.py`:
   - Executes a pipeline synchronously within a workflow trigger/step
   - Stores pipeline outputs in a workflow variable (`result_variable`)
   - Handles approval gates by returning control to the hook (non-blocking wait)

2. **New `spawn_session` pipeline step type** in `pipeline_executor.py`:
   - Add `spawn_session` field to `PipelineStep` model in `definitions.py`
   - Uses existing terminal spawner infrastructure (`src/gobby/agents/spawner/`)
   - Creates session, activates workflow, waits for `exit_condition` or timeout
   - Workflow's final variables become step output

3. **New `activate_workflow` pipeline step type** in `pipeline_executor.py`:
   - Add `activate_workflow` field to `PipelineStep` model
   - Activates workflow on the pipeline's current session
   - Non-blocking by default; optional `wait_for` blocks until exit condition met

4. **Update `PipelineStep` validation** in `definitions.py:242`:
   - Add `spawn_session` and `activate_workflow` to mutually exclusive execution types

**Files:**

| File | Change |
|------|--------|
| `src/gobby/workflows/actions.py` | Add `run_pipeline` action handler |
| `src/gobby/workflows/pipeline_executor.py` | Add `spawn_session`, `activate_workflow` step execution |
| `src/gobby/workflows/definitions.py` | Add new step type fields to `PipelineStep` |

---

### Phase 7: YAML Migration

**Goal:** Migrate all existing workflow YAMLs to the unified format.

**Current active workflows (13 files):**

| File | Current type | New format |
|------|-------------|------------|
| `lifecycle/session-lifecycle.yaml` | `type: lifecycle` | `enabled: true`, `priority: 10` |
| `lifecycle/headless-lifecycle.yaml` | `type: lifecycle` | `enabled: true`, `priority: 10` |
| `auto-task.yaml` | `type: step` | `enabled: false`, `priority: 25` |
| `developer.yaml` | `type: step` | `enabled: false`, `priority: 20` |
| `generic.yaml` | `type: step` | `enabled: false` |
| `coordinator.yaml` | `type: step` | `enabled: false` |
| `code-review.yaml` | `type: step` | `enabled: false` |
| `merge.yaml` | `type: step` | `enabled: false` |
| `qa-reviewer.yaml` | `type: step` | `enabled: false` |
| `meeseeks-box.yaml` | `type: step` | `enabled: false` |
| `meeseeks-box-pipeline.yaml` | `type: pipeline` | No change (pipelines unchanged) |

**Migration rules:**
1. `type: lifecycle` → remove `type`, add `enabled: true`, move `settings.priority` to top-level `priority`
2. `type: step` → remove `type`, add `enabled: false`
3. `type: pipeline` → no change
4. Move `settings.priority: N` → `priority: N` (top-level)
5. Split `variables:` into `variables:` (workflow-scoped) and `session_variables:` (shared)
   - For `session-lifecycle.yaml`: most variables become `session_variables` (e.g., `unlocked_tools`, `task_claimed`, `servers_listed`)
   - For step workflows: variables that are read/written only by that workflow stay as `variables`
6. Move files from `lifecycle/` subdirectory to root `workflows/` directory

**Condition expression changes:**

| Current | New | Context |
|---------|-----|---------|
| `variables.get('key')` | `session.key` or `variables.key` | Depends on scoping |
| `task_claimed` (flattened) | `session.task_claimed` | Session variable |
| `variables.get('enforce_tool_schema_check')` | `session.enforce_tool_schema_check` | Session variable |
| `not variables.get('servers_listed')` | `not session.servers_listed` | Session variable |
| `variables.get('stop_attempts', 0)` | `session.stop_attempts` | Session variable |

**Breaking changes:**
- Condition expressions using `variables.get('session_var')` must change to `session.session_var`
- Custom user workflows in `~/.gobby/workflows/lifecycle/` must move to `~/.gobby/workflows/`
- The `type` field is ignored — workflows with `type: lifecycle` still work but should be updated

---

## Part 6: Files to Modify — Complete Manifest

### New files

| File | Purpose |
|------|---------|
| `src/gobby/workflows/unified_evaluator.py` | Single evaluation loop for all workflows |

### Modified files

| File | Lines | What changes |
|------|-------|-------------|
| **Storage** | | |
| `src/gobby/storage/migrations.py` | ~1220 | Add v99: `workflow_instances` + `session_variables` tables + data migration |
| **Workflow definitions** | | |
| `src/gobby/workflows/definitions.py` | 136-184 | Add `enabled`, `priority`, `session_variables` to `WorkflowDefinition`; add `WorkflowInstance` model; add `spawn_session`/`activate_workflow` to `PipelineStep` |
| **State management** | | |
| `src/gobby/workflows/state_manager.py` | 1-305 | Add `WorkflowInstanceManager`, `SessionVariableManager`; deprecate `WorkflowStateManager` methods |
| **Engine** | | |
| `src/gobby/workflows/engine.py` | 105-1120 | Delegate to `unified_evaluator.py`; keep `WorkflowEngine` class as facade |
| `src/gobby/workflows/lifecycle_evaluator.py` | 514-741 | Delegate to `unified_evaluator.py`; keep functions as deprecated wrappers |
| **Loader** | | |
| `src/gobby/workflows/loader.py` | 698-787 | Rename `discover_lifecycle_workflows` → `discover_workflows`; flatten directory scanning; build trigger index |
| **Hook integration** | | |
| `src/gobby/workflows/hooks.py` | 51-93 | Rename `handle_all_lifecycles` → `evaluate`; remove `handle()` step-only method |
| `src/gobby/hooks/event_handlers/_base.py` | (new method) | Add `_evaluate_workflows()` helper |
| `src/gobby/hooks/event_handlers/_agent.py` | 58, 205, 237, 277 | Replace 4 inline workflow blocks with `_evaluate_workflows()` |
| `src/gobby/hooks/event_handlers/_session.py` | 264, 328, 461 | Replace 3 inline workflow blocks with `_evaluate_workflows()` |
| `src/gobby/hooks/event_handlers/_tool.py` | 35, 104 | Replace 2 inline workflow blocks with `_evaluate_workflows()` |
| `src/gobby/servers/websocket/chat.py` | 172-174 | Update to unified handler |
| **MCP tools** | | |
| `src/gobby/mcp_proxy/tools/workflows/_lifecycle.py` | 21-188 | Multi-workflow activation; per-workflow `end_workflow` |
| `src/gobby/mcp_proxy/tools/workflows/_variables.py` | 21-193 | Scoped variables; new `set_session_variable` |
| `src/gobby/mcp_proxy/tools/workflows/_query.py` | (update) | Multi-workflow status |
| `src/gobby/mcp_proxy/tools/workflows/__init__.py` | (update) | Register new tools |
| **Pipeline** | | |
| `src/gobby/workflows/pipeline_executor.py` | (new methods) | Add `_execute_spawn_session`, `_execute_activate_workflow` step handlers |
| `src/gobby/workflows/actions.py` | (new handler) | Add `run_pipeline` action |
| **Workflow YAMLs** | | |
| `src/gobby/install/shared/workflows/lifecycle/session-lifecycle.yaml` | all | Migrate to unified format; split variables |
| `src/gobby/install/shared/workflows/lifecycle/headless-lifecycle.yaml` | all | Migrate to unified format; split variables |
| `src/gobby/install/shared/workflows/auto-task.yaml` | top | Remove `type`, add `enabled: false` |
| `src/gobby/install/shared/workflows/developer.yaml` | top | Remove `type`, add `enabled: false` |
| `src/gobby/install/shared/workflows/generic.yaml` | top | Remove `type`, add `enabled: false` |
| `src/gobby/install/shared/workflows/coordinator.yaml` | top | Remove `type`, add `enabled: false` |
| `src/gobby/install/shared/workflows/code-review.yaml` | top | Remove `type`, add `enabled: false` |
| `src/gobby/install/shared/workflows/merge.yaml` | top | Remove `type`, add `enabled: false` |
| `src/gobby/install/shared/workflows/qa-reviewer.yaml` | top | Remove `type`, add `enabled: false` |
| `src/gobby/install/shared/workflows/meeseeks-box.yaml` | top | Remove `type`, add `enabled: false` |

---

## Part 7: Migration Details

### Database migration v99

```python
def _migrate_v99(db: HubDatabase) -> None:
    """Migrate workflow_states to workflow_instances + session_variables."""

    with db.transaction():
        # 1. Create new tables
        db.execute("""
            CREATE TABLE workflow_instances (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                workflow_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                priority INTEGER NOT NULL DEFAULT 100,
                current_step TEXT,
                step_entered_at TEXT,
                step_action_count INTEGER DEFAULT 0,
                total_action_count INTEGER DEFAULT 0,
                variables TEXT DEFAULT '{}',
                context_injected INTEGER DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(session_id, workflow_name),
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
        """)
        db.execute("CREATE INDEX idx_wi_session ON workflow_instances(session_id)")
        db.execute("CREATE INDEX idx_wi_enabled ON workflow_instances(session_id, enabled)")

        db.execute("""
            CREATE TABLE session_variables (
                session_id TEXT PRIMARY KEY,
                variables TEXT DEFAULT '{}',
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # 2. Migrate existing data
        rows = db.fetchall("SELECT * FROM workflow_states")
        for row in rows:
            session_id = row["session_id"]
            workflow_name = row["workflow_name"]
            variables = json.loads(row["variables"]) if row["variables"] else {}

            # All existing variables become session variables (backward compat)
            db.execute(
                "INSERT OR IGNORE INTO session_variables (session_id, variables, updated_at) VALUES (?, ?, ?)",
                (session_id, json.dumps(variables), row["updated_at"]),
            )

            # Create workflow instance for active step workflows
            if workflow_name not in ("__lifecycle__", "__ended__"):
                import uuid
                db.execute(
                    """INSERT OR IGNORE INTO workflow_instances
                       (id, session_id, workflow_name, enabled, current_step,
                        step_entered_at, step_action_count, total_action_count,
                        variables, context_injected, updated_at)
                       VALUES (?, ?, ?, 1, ?, ?, ?, ?, '{}', ?, ?)""",
                    (
                        str(uuid.uuid4()), session_id, workflow_name,
                        row["step"], row["step_entered_at"],
                        row["step_action_count"], row["total_action_count"],
                        row["context_injected"], row["updated_at"],
                    ),
                )
```

### YAML migration rules

**session-lifecycle.yaml** variable split:

```yaml
# BEFORE (current)
variables:
  debug_echo_context: true
  require_task_before_edit: true
  require_commit_before_close: true
  clear_task_on_close: true
  require_uv: true
  enforce_tool_schema_check: true
  unlocked_tools: []
  servers_listed: false
  listed_servers: []
  pre_existing_errors_triaged: false
  stop_attempts: 0
  max_stop_attempts: 3

# AFTER (unified)
session_variables:
  debug_echo_context: true
  require_task_before_edit: true
  require_commit_before_close: true
  clear_task_on_close: true
  require_uv: true
  enforce_tool_schema_check: true
  unlocked_tools: []
  servers_listed: false
  listed_servers: []
  pre_existing_errors_triaged: false
  stop_attempts: 0
  max_stop_attempts: 3
# (no workflow-scoped variables needed for this workflow)
```

**auto-task.yaml** variable split:

```yaml
# BEFORE
variables:
  session_task: null
  context_injected: false

# AFTER
variables:            # Workflow-scoped (cleared when deactivated)
  context_injected: false
session_variables:    # Shared (persists across workflow activations)
  session_task: null
```

### Backward compatibility

| Scenario | Handling |
|----------|---------|
| YAML with `type: lifecycle` | Treated as `enabled: true`. Warning logged, no error. |
| YAML with `type: step` | Treated as `enabled: false`. Warning logged, no error. |
| Condition `variables.get('session_var')` | Still works — evaluator checks both workflow + session variables for `variables.*` access. Deprecation warning logged. |
| `set_variable(name, value)` without `workflow` | Writes to session variables (backward compat). Deprecation warning. |
| Workflows in `lifecycle/` subdirectory | Still discovered. Warning to migrate to flat directory. |
| Old `workflow_states` table | Kept until v101 migration (data already migrated to new tables). |

---

## Part 8: Verification

### Phase 1 verification (State Model)

- [ ] Migration v99 applies cleanly on fresh database
- [ ] Migration v99 migrates existing `workflow_states` data correctly
- [ ] `WorkflowInstanceManager` CRUD operations work (create, get, list, update, delete)
- [ ] `SessionVariableManager` CRUD operations work
- [ ] Atomic `merge_variables` prevents concurrent write corruption
- [ ] Multiple workflow instances can exist for one session
- [ ] `UNIQUE(session_id, workflow_name)` constraint prevents duplicates

### Phase 2 verification (Definition Schema)

- [ ] Old YAMLs with `type: lifecycle/step` still parse without error
- [ ] New `enabled`/`priority`/`session_variables` fields parse correctly
- [ ] `discover_workflows()` finds workflows in both `lifecycle/` and root directories
- [ ] Priority ordering works correctly (lower number = runs first)
- [ ] Source filtering still works (`sources: [claude]`)

### Phase 3 verification (Unified Engine)

- [ ] Single evaluation loop produces same results as current dual-path
- [ ] Triggers fire correctly for all event types
- [ ] Step tool restrictions still enforced
- [ ] Step transitions still work (including auto-transition chains)
- [ ] Multiple concurrent workflows: context accumulates, first block wins
- [ ] Priority ordering respected during evaluation
- [ ] `session.*` conditions evaluate against session variables
- [ ] `variables.*` conditions evaluate against workflow-scoped variables
- [ ] Performance: trigger index prevents evaluating irrelevant workflows

### Phase 4 verification (Hook Integration)

- [ ] All 10 call sites produce identical behavior after consolidation
- [ ] Error handling preserved (try/except around workflow evaluation)
- [ ] WebSocket chat path works with unified handler
- [ ] No regressions in hook event flow

### Phase 5 verification (MCP Tools)

- [ ] `activate_workflow()` can activate multiple workflows simultaneously
- [ ] `end_workflow(workflow="name")` ends specific workflow without affecting others
- [ ] `set_variable(name, value, workflow="name")` writes to workflow-scoped storage
- [ ] `set_session_variable(name, value)` writes to session-scoped storage
- [ ] Backward compat: `set_variable()` without `workflow` writes to session variables
- [ ] Workflow status tool reports all active instances

### Phase 6 verification (Pipeline Composability)

- [ ] `run_pipeline` action in workflow trigger executes pipeline and stores result
- [ ] `spawn_session` pipeline step spawns terminal, activates workflow, waits for completion
- [ ] `activate_workflow` pipeline step activates on current session
- [ ] `wait_for: exit_condition` blocks pipeline until workflow completes
- [ ] Timeout prevents infinite pipeline waits
- [ ] Workflow final variables become pipeline step output

### Phase 7 verification (YAML Migration)

- [ ] Migrated `session-lifecycle.yaml` produces identical behavior
- [ ] Migrated `headless-lifecycle.yaml` produces identical behavior
- [ ] All step workflows activate/deactivate correctly with new format
- [ ] No condition expression regressions (run full hook test suite)
- [ ] User workflows in `~/.gobby/workflows/lifecycle/` still discovered (with deprecation warning)

### End-to-end verification

1. **Fresh install**: New database with v99 schema, all workflows load and evaluate correctly
2. **Upgrade path**: Existing database migrates cleanly, no behavior changes
3. **Multi-workflow session**: Activate `session-lifecycle` (always-on) + `auto-task` + `developer` simultaneously — priorities respected, variables isolated, first block wins
4. **Pipeline → workflow**: Pipeline step spawns agent with `developer` workflow, waits for completion, receives final variables
5. **Workflow → pipeline**: Workflow step action runs `ci-checks` pipeline, stores result, transitions based on outcome
6. **Variable isolation**: Writing `variables.x` in workflow A does not affect workflow B or session variables
7. **Backward compat**: Old YAML format still works without modification (deprecation warnings only)
