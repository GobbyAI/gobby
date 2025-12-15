# Workflow Engine Plan

## Vision

Transform Gobby from a passive session tracker into an **enforcement layer** for deterministic AI agent workflows. Instead of relying on prompts to guide LLM behavior, workflows use hooks to enforce phases, tool restrictions, and transitions.

Key insight: **The LLM doesn't need to remember what phase it's in** - the workflow engine tracks state and hooks enforce it. The LLM sees tool blocks and injected context that guide it naturally.

Inspired by:

- [Parlant](https://github.com/emcie-co/parlant) - Behavioral enforcement over prompts
- [BMAD-METHOD](https://github.com/bmad-code-org/BMAD-METHOD) - YAML workflows with phases and agents

## Supported Patterns

### Plan-and-Execute

LLM plans steps first, then executes with validation loops for adjustments.

```text
plan → execute → validate ⟲ (loop on failure)
```

### ReAct (Reasoning + Acting)

Continuous reason-observe-act cycle with implicit validation via observation.

```text
reason → act → observe → reason (continuous cycle)
```

### Plan-Act-Reflect

Adds explicit critique/validate phase before replanning, common in coding agents.

```text
plan → act → reflect → replan (critique before continuing)
```

---

## Workflow Types

### Lifecycle Workflows

Event-driven workflows that respond to session events without enforcing phases or tool restrictions. They execute actions based on triggers.

**Use cases:**

- Session handoff (current behavior)
- Auto-save / backup
- Logging and analytics
- Notifications

**Characteristics:**

- `type: lifecycle`
- No `phases` section
- Only `triggers` section
- Actions execute in sequence per event
- Multiple lifecycle workflows can be active

### Phase-Based Workflows

State machine workflows that enforce phases with tool restrictions, transition conditions, and exit criteria.

**Use cases:**

- Plan-and-Execute
- ReAct
- Plan-Act-Reflect
- TDD

**Characteristics:**

- `type: phase` (default)
- Has `phases` section with allowed/blocked tools
- Has `transitions` and `exit_conditions`
- Only one phase-based workflow active per session
- Can coexist with lifecycle workflows

---

## Architecture

### Components

```text
┌─────────────────────────────────────────────────────────────┐
│                      Hook Events                             │
│  session_start | prompt_submit | tool_call | tool_result    │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                   Workflow Engine                            │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   Loader    │  │   State     │  │   Rule Evaluator    │  │
│  │  (YAML)     │  │   Manager   │  │   (per hook type)   │  │
│  └─────────────┘  └─────────────┘  └─────────────────────┘  │
└─────────────────────────┬───────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                   Hook Response                              │
│  continue | block (message) | modify (inject context)       │
└─────────────────────────────────────────────────────────────┘
```

### File Locations

| Location | Purpose |
|----------|---------|
| `~/.gobby/workflows/` | Global workflow definitions |
| `.gobby/workflows/` | Project-specific workflows |
| `~/.gobby/workflows/templates/` | Built-in templates (Plan-Execute, ReAct, etc.) |
| Session record | Workflow state (current phase, action count, etc.) |

### Workflow State

```python
@dataclass
class WorkflowState:
    workflow_name: str           # "plan-act-reflect"
    phase: str                   # Current phase: "plan", "act", "reflect"
    phase_entered_at: datetime   # When current phase started
    phase_action_count: int      # Actions taken in current phase
    total_action_count: int      # Total actions in session
    artifacts: dict[str, str]    # Named artifacts: {"current_plan": "...content..."}
    observations: list[dict]     # Observation buffer for ReAct pattern
    reflection_pending: bool     # Must reflect before continuing
    context_injected: bool       # Has handoff/summary been injected
    variables: dict[str, Any]    # User-defined workflow variables

    # Task decomposition state (for plan-to-tasks workflow)
    task_list: list[dict] | None      # Decomposed tasks with verification criteria
    current_task_index: int           # Index of current task being executed
    files_modified_this_task: int     # Track scope creep

    # NOTE: Workflow state is intentionally local-only.
    # Cross-session continuity is achieved by persisting work to the task system (tasks.jsonl),
    # not by syncing ephemeral workflow state.
```

---

## Workflow Definition Format

### Complete Example

```yaml
name: plan-act-reflect
description: "Structured development with planning and reflection phases"
version: "1.0"

# Workflow-level settings
settings:
  reflect_after_actions: 5        # Trigger reflection after N actions
  max_actions_per_phase: 20       # Safety limit
  stuck_detection:
    max_phase_duration_minutes: 30 # Auto-transition to reflect if stuck
    max_verification_attempts: 3   # Auto-proceed after N failed verifications
  require_plan_approval: true     # User must approve plan before acting

# Variable definitions (can be overridden per-project)
variables:
  plan_file_pattern: "**/*.plan.md"
  allowed_test_commands: ["pytest", "npm test", "cargo test"]

# Phase definitions
phases:
  - name: plan
    description: "Analyze requirements and create implementation plan"

    on_enter:
      - action: inject_context
        source: previous_session_summary
        when: has_previous_session
      - action: inject_context
        source: handoff
        when: has_handoff
      - action: switch_mode
        mode: plan
      - action: inject_message
        content: |
          You are in PLANNING mode. Analyze the request and create a plan.
          Do not modify any files until the plan is approved.

    allowed_tools:
      - Read
      - Glob
      - Grep
      - WebSearch
      - WebFetch
      - Task          # Exploration agents only
      - AskUserQuestion
      - TodoWrite

    blocked_tools:
      - Edit
      - Write
      - Bash
      - NotebookEdit

    exit_conditions:
      # All conditions must be met (AND logic)
      - type: artifact_exists
        pattern: "{{ plan_file_pattern }}"
      - type: user_approval
        prompt: "Plan complete. Ready to implement?"

    on_exit:
      - action: capture_artifact
        pattern: "{{ plan_file_pattern }}"
        as: current_plan

  - name: act
    description: "Implement the plan"

    on_enter:
      - action: inject_message
        content: |
          You are now in IMPLEMENTATION mode. Follow the plan:
          {{ artifacts.current_plan }}

    allowed_tools: all

    blocked_tools: []

    # Inline rules evaluated on each tool call
    rules:
      - when: "tool == 'Edit' and file not in session.files_read"
        action: block
        message: "Read the file before editing: {{ file }}"

      - when: "tool == 'Bash' and command_contains('rm -rf')"
        action: block
        message: "Destructive commands require explicit approval"

      - when: "tool == 'Bash' and not command_in(allowed_test_commands)"
        action: require_approval
        message: "Non-standard command: {{ command }}"

    # Transition triggers
    transitions:
      - to: reflect
        when: "phase_action_count >= reflect_after_actions"
      - to: reflect
        when: "tool_result.is_error"

  - name: reflect
    description: "Review progress and decide next steps"

    on_enter:
      - action: inject_message
        content: |
          REFLECTION CHECKPOINT

          Actions taken: {{ phase_action_count }}
          Files modified: {{ session.files_modified }}
          Errors encountered: {{ session.errors }}

          Review your progress against the plan. Either:
          1. Continue implementing (transition to 'act')
          2. Revise the plan (transition to 'plan')
          3. Mark task complete (end workflow)

    allowed_tools:
      - Read
      - Glob
      - Grep
      - TodoWrite
      - AskUserQuestion

    blocked_tools:
      - Edit
      - Write
      - Bash

    transitions:
      - to: act
        when: "user_says('continue') or user_says('proceed')"
      - to: plan
        when: "user_says('revise') or user_says('replan')"
      - to: complete
        when: "user_says('done') or user_says('complete')"

# Workflow-level triggers (apply to all phases)
triggers:
  on_session_start:
    - action: load_workflow_state
    - action: restore_from_handoff
      when: has_handoff
    - action: enter_phase
      phase: plan
      when: "not workflow_state.phase"  # Only if no existing state

  on_session_end:
    - action: save_workflow_state
    - action: generate_handoff
      include:
        - current_phase
        - artifacts
        - pending_tasks

# Error handling
on_error:
  - action: enter_phase
    phase: reflect
  - action: inject_message
    content: "An error occurred. Entering reflection to assess."
```

### Minimal Example

```yaml
name: simple-plan-execute
description: "Basic planning enforcement"

phases:
  - name: plan
    allowed_tools: [Read, Glob, Grep, WebSearch]
    exit_conditions:
      - type: user_approval
        prompt: "Ready to implement?"

  - name: execute
    allowed_tools: all

triggers:
  on_session_start:
    - action: enter_phase
      phase: plan
```

---

## Hook Integration

### Hook → Workflow Mapping

| Hook Event | Workflow Actions |
|------------|------------------|
| `session_start` | Initialize state, restore handoff, enter initial phase |
| `prompt_submit` | Inject phase context, check pending reflection |
| `tool_call` | Validate tool allowed, evaluate rules, check transitions |
| `tool_result` | Capture observations, update action count, check error transitions |
| `assistant_response` | Check artifact creation, evaluate exit conditions |
| `session_end` | Save state, generate handoff |

### Hook Response Types

```python
class HookResponse:
    action: Literal["continue", "block", "modify"]
    message: str | None           # Shown to user on block
    inject_context: str | None    # Prepended to next prompt
    modify_request: dict | None   # Modifications to the request
```

### Evaluation Flow

```python
async def evaluate_workflow(event: HookEvent) -> HookResponse:
    # 1. Load workflow and state
    workflow = load_workflow(event.session_id)
    state = get_workflow_state(event.session_id)

    if not workflow:
        return HookResponse(action="continue")

    phase = workflow.get_phase(state.phase)

    # 2. Check tool permissions (for tool_call events)
    if event.type == "tool_call":
        if event.tool_name in phase.blocked_tools:
            return HookResponse(
                action="block",
                message=f"Tool '{event.tool_name}' not allowed in {state.phase} phase. "
                        f"Allowed: {phase.allowed_tools}"
            )

        # 3. Evaluate phase rules
        for rule in phase.rules:
            if evaluate_condition(rule.when, event, state):
                if rule.action == "block":
                    return HookResponse(action="block", message=rule.message)
                elif rule.action == "require_approval":
                    # Could prompt user or inject approval request
                    pass

    # 4. Check transitions
    for transition in phase.transitions:
        if evaluate_condition(transition.when, event, state):
            await enter_phase(state, transition.to, workflow)
            return HookResponse(
                action="modify",
                inject_context=f"[Transitioned to {transition.to} phase]"
            )

    # 5. Check exit conditions
    if all(check_condition(c, state) for c in phase.exit_conditions):
        next_phase = workflow.get_next_phase(state.phase)
        if next_phase:
            await enter_phase(state, next_phase.name, workflow)

    return HookResponse(action="continue")
```

---

## Built-in Templates

### 1. plan-execute.yaml

Basic planning enforcement. Plan phase restricts to read-only tools until user approves.

### 2. react.yaml

ReAct loop with observation capture. Each action's result is captured and injected into reasoning context.

### 3. plan-act-reflect.yaml

Full reflection workflow. Automatically enters reflection phase after N actions or on errors.

### 4. architect.yaml

BMAD-inspired development workflow. Phases: requirements → design → implementation → review.

### 5. test-driven.yaml

TDD workflow. Phases: write-test → implement → refactor. Blocks implementation until test exists.

### 6. plan-to-tasks.yaml

Task decomposition workflow. Takes a completed plan and breaks it into atomic, sequential tasks with verification criteria. Executes tasks one at a time with verification gates.

```yaml
name: plan-to-tasks
description: "Decompose a plan into atomic tasks and execute sequentially"
type: phase

variables:
  plan_pattern: "**/*.plan.md"
  max_tasks: 20
  require_task_approval: false

phases:
  - name: decompose
    description: "Break plan into atomic tasks"

    on_enter:
      - action: read_artifact
        pattern: "{{ plan_pattern }}"
        as: current_plan

      - action: call_llm
        prompt: |
          Break this plan into atomic, sequential tasks.
          Each task should be:
          - Single responsibility (one file, one function, one test)
          - Independently verifiable
          - Ordered by dependency

          Plan:
          {{ current_plan }}

          Output as JSON: {"tasks": [{"id": 1, "description": "...", "verification": "..."}]}
        output_as: task_list

      - action: persist_tasks
        source: task_list.tasks
        create_dependencies: sequential
        link_to_session: true

      - action: write_todos  # Mirror to Claude Code UI
        source: task_list.tasks

      - action: inject_message
        content: |
          Decomposed plan into {{ task_list.tasks | length }} tasks:
          {% for task in task_list.tasks %}
          {{ task.id }}. {{ task.description }}
          {% endfor %}

    allowed_tools:
      - Read
      - Glob
      - TodoWrite

    exit_conditions:
      - type: variable_set
        variable: task_list
      - type: user_approval
        when: "{{ require_task_approval }}"
        prompt: "Proceed with these {{ task_list.tasks | length }} tasks?"

  - name: execute
    description: "Work through tasks sequentially"

    on_enter:
      - action: set_variable
        name: current_task_index
        value: 0

      - action: inject_message
        content: |
          Starting task {{ current_task_index + 1 }}/{{ task_list.tasks | length }}:
          {{ task_list.tasks[current_task_index].description }}

          Verification: {{ task_list.tasks[current_task_index].verification }}

    allowed_tools: all

    rules:
      # Prevent working on multiple tasks at once
      - when: "files_modified_this_task > 3"
        action: warn
        message: "Multiple files modified. Ensure you're focused on current task only."

    transitions:
      - to: verify
        when: "user_says('done') or user_says('next')"

  - name: verify
    description: "Verify current task completion"

    on_enter:
      - action: inject_message
        content: |
          Verify task {{ current_task_index + 1 }} is complete:

          Task: {{ task_list.tasks[current_task_index].description }}
          Verification: {{ task_list.tasks[current_task_index].verification }}

          Run any tests or checks, then confirm.

    allowed_tools:
      - Read
      - Glob
      - Grep
      - Bash  # For running tests
      - TodoWrite

    blocked_tools:
      - Edit
      - Write

    transitions:
      - to: execute
        when: "verification_passed and current_task_index < task_list.tasks | length - 1"
        on_transition:
          - action: mark_todo_complete
            index: "{{ current_task_index }}"
          - action: increment_variable
            name: current_task_index
          - action: inject_message
            content: "Task {{ current_task_index }} verified. Moving to task {{ current_task_index + 1 }}."

      - to: complete
        when: "verification_passed and current_task_index >= task_list.tasks | length - 1"

      - to: execute
        when: "verification_failed"
        on_transition:
          - action: inject_message
            content: "Verification failed. Returning to execute phase to fix issues."

  - name: complete
    description: "All tasks complete"

    on_enter:
      - action: mark_todo_complete
        index: "{{ current_task_index }}"

      - action: inject_message
        content: |
          All {{ task_list.tasks | length }} tasks completed!

          Summary:
          {% for task in task_list.tasks %}
          ✓ {{ task.description }}
          {% endfor %}
```

**Flow:**

```text
decompose → execute → verify ⟲ (loop until all tasks done) → complete
           ↑__________|
```

**Key features:**

- LLM-powered task decomposition with verification criteria
- TodoWrite integration for task tracking
- Verification gate between tasks
- Prevents scope creep with file modification warnings

### 7. session-handoff.yaml (Lifecycle Workflow)

Extracts the current session summary and handoff system as a workflow. This is a **lifecycle workflow** (no phases, just event responses) rather than a **phase-based workflow**.

```yaml
name: session-handoff
description: "Session summary generation and context handoff between sessions"
type: lifecycle  # No phases, responds to events

triggers:
  on_session_start:
    - action: find_parent_session
      when: "trigger_source == 'clear'"
      filter:
        status: handoff_ready
        same_project: true
        same_machine: true

    - action: restore_context
      source: parent_session_summary
      when: parent_session_found
      inject_as: system_context

    - action: mark_session_status
      target: parent
      status: expired
      when: context_restored

  on_session_end:
    - action: generate_summary
      generator: llm
      template: default  # or custom prompt

    - action: mark_session_status
      status: handoff_ready

  on_prompt_submit:
    - action: synthesize_title
      when: "session.title == null"
      generator: llm

    # Handle /clear as early handoff
    - action: generate_summary
      when: "prompt.strip().lower() == '/clear'"
      generator: llm
```

This demonstrates that:

1. **Lifecycle workflows** respond to events without enforcing phases
2. **Phase-based workflows** (plan-act-reflect, etc.) enforce tool restrictions and transitions
3. Both types can be active simultaneously on a session

---

## Context Injection

### Sources

| Source | Description |
|--------|-------------|
| `previous_session_summary` | LLM-generated summary from last session |
| `handoff` | Structured handoff data (phase, artifacts, pending tasks) |
| `artifacts` | Contents of captured artifacts (plans, specs) |
| `observations` | ReAct observation buffer |
| `workflow_state` | Current state as structured data |

### Injection Points

1. **on_enter**: When entering a phase
2. **on_prompt_submit**: Before each user prompt is processed
3. **on_transition**: When moving between phases

### Example Injection

```yaml
on_enter:
  - action: inject_context
    source: previous_session_summary
    template: |
      ## Previous Session Context
      {{ summary }}

      ## Handoff Notes
      {{ handoff.notes }}

      ## Pending Tasks
      {% for task in handoff.pending_tasks %}
      - {{ task }}
      {% endfor %}
```

---

## Storage Schema

### Workflow State Table

```sql
CREATE TABLE workflow_states (
    session_id TEXT PRIMARY KEY,
    workflow_name TEXT NOT NULL,
    phase TEXT NOT NULL,
    phase_entered_at TIMESTAMP,
    phase_action_count INTEGER DEFAULT 0,
    total_action_count INTEGER DEFAULT 0,
    artifacts JSON,           -- ["plan.md", "spec.md"]
    observations JSON,        -- ReAct observation buffer
    reflection_pending BOOLEAN DEFAULT FALSE,
    variables JSON,           -- User-defined variables
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
```

### Handoff Table

```sql
CREATE TABLE workflow_handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    workflow_name TEXT NOT NULL,
    from_session_id TEXT,
    phase TEXT,
    artifacts JSON,
    pending_tasks JSON,
    notes TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    consumed_at TIMESTAMP,      -- NULL until used
    consumed_by_session TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);
```

---

## CLI Commands

```bash
# List available workflows
gobby workflow list

# Show workflow details
gobby workflow show plan-act-reflect

# Set workflow for current project
gobby workflow set plan-act-reflect

# Clear workflow for current project
gobby workflow clear

# Show current workflow state
gobby workflow status

# Manually transition phase (escape hatch)
gobby workflow phase <phase-name>

# Create handoff for next session
gobby workflow handoff "Notes about current state"

# Import workflow from URL or file
gobby workflow import https://example.com/workflow.yaml
gobby workflow import ./my-workflow.yaml

# Escape Hatches & Debugging
gobby workflow phase <name> --force     # Skip exit conditions
gobby workflow reset                    # Return to initial phase
gobby workflow disable                  # Temporarily suspend enforcement
```

---

## MCP Tool Integration

### Workflow Tools for LLM

Expose workflow controls as MCP tools so the LLM can interact with workflows:

```python
@mcp.tool()
async def get_workflow_status() -> dict:
    """Get current workflow phase and state."""

@mcp.tool()
async def request_phase_transition(to_phase: str, reason: str) -> dict:
    """Request transition to a different phase. May require approval."""

@mcp.tool()
async def create_handoff(notes: str, pending_tasks: list[str]) -> dict:
    """Create a handoff for the next session."""

@mcp.tool()
async def mark_artifact_complete(artifact_type: str, file_path: str) -> dict:
    """Register an artifact as complete (plan, spec, etc.)."""
```

### Tool Filtering

When tools are restricted by phase, the MCP tool list should reflect this:

```python
async def list_tools(session_id: str) -> list[Tool]:
    all_tools = get_all_mcp_tools()
    workflow = get_workflow(session_id)

    if not workflow:
        return all_tools

    phase = workflow.current_phase

    if phase.allowed_tools == "all":
        blocked = set(phase.blocked_tools)
        return [t for t in all_tools if t.name not in blocked]
    else:
        allowed = set(phase.allowed_tools)
        return [t for t in all_tools if t.name in allowed]
```

---

## Implementation Checklist

### Phase 0: Extract Current Handoff System

Before building new workflow capabilities, extract the current session handoff behavior into a workflow definition. This validates the workflow schema can express existing functionality.

- [ ] Create `templates/session-handoff.yaml` with current behavior
- [ ] Map `_handle_event_session_start` logic to workflow triggers
- [ ] Map `_handle_event_session_end` logic to workflow triggers
- [ ] Map `_handle_event_before_agent` title synthesis to workflow trigger
- [ ] Document action types needed: `find_parent_session`, `restore_context`, `mark_session_status`, `generate_summary`, `synthesize_title`
- [ ] Identify any gaps between current code and workflow expressiveness

### Phase 1: Foundation

- [ ] Create `src/workflows/` module structure
- [ ] Define `WorkflowDefinition` dataclass (parsed YAML representation)
- [ ] Define `WorkflowState` dataclass
- [ ] Implement `WorkflowLoader` to parse YAML workflow files
- [ ] Add workflow state columns to sessions table (migration)
- [ ] Create `workflow_states` table (migration)
- [ ] Create `workflow_handoffs` table (migration)
- [ ] Implement `WorkflowStateManager` for CRUD operations on state

### Phase 2: Core Engine

- [ ] Implement `WorkflowEngine` class with phase management
- [ ] Implement condition evaluator for `when` clauses
- [ ] Implement `enter_phase()` with on_enter action execution
- [ ] Implement `exit_phase()` with on_exit action execution
- [ ] Implement tool permission checking (allowed/blocked lists)
- [ ] Implement rule evaluation for inline phase rules
- [ ] Implement transition evaluation and execution
- [ ] Implement exit condition checking
- [ ] Implement "Dual Write" pattern (TodoWrite + create_task)
- [ ] Implement stuck detection (duration & attempt limits)
- [ ] Optimize Rule Evaluator (pre-compile conditions, short-circuit, cache state)

### Phase 3: Hook Integration

- [ ] Create `WorkflowHookHandler` that wraps existing hook system
- [ ] Integrate workflow evaluation into `on_session_start` hook
- [ ] Integrate workflow evaluation into `on_prompt_submit` hook
- [ ] Integrate workflow evaluation into `on_tool_call` hook
- [ ] Integrate workflow evaluation into `on_tool_result` hook
- [ ] Integrate workflow evaluation into `on_session_end` hook
- [ ] Implement `HookResponse` with block/modify/continue actions
- [ ] Add context injection to hook responses

### Phase 4: Actions

**Context & Messaging:**

- [ ] Implement `inject_context` action
- [ ] Implement `inject_message` action
- [ ] Implement `switch_mode` action (for Claude Code plan mode)

**Artifacts:**

- [ ] Implement `capture_artifact` action
- [ ] Implement `read_artifact` action (load file content into variable)

**State Management:**

- [ ] Implement `load_workflow_state` action
- [ ] Implement `save_workflow_state` action
- [ ] Implement `set_variable` action
- [ ] Implement `increment_variable` action

**Handoff:**

- [ ] Implement `generate_handoff` action
- [ ] Implement `restore_from_handoff` action
- [ ] Implement `find_parent_session` action
- [ ] Implement `mark_session_status` action

**LLM Integration:**

- [ ] Implement `call_llm` action (invoke LLM with prompt template)
- [ ] Implement `generate_summary` action
- [ ] Implement `synthesize_title` action

**TodoWrite Integration:**

- [ ] Implement `write_todos` action (populate TodoWrite from task list)
- [ ] Implement `mark_todo_complete` action

**Task System Integration:**

- [ ] Implement `persist_tasks` action (create tasks with dependencies, session linking)

**MCP Tool Invocation:**

- [ ] Implement `call_mcp_tool` action (invoke any gobby MCP tool by name)

### Phase 5: Context Sources

- [ ] Implement `previous_session_summary` context source
- [ ] Implement `handoff` context source
- [ ] Implement `artifacts` context source
- [ ] Implement `observations` context source (ReAct buffer)
- [ ] Implement `workflow_state` context source
- [ ] Add Jinja2 templating for context injection

### Phase 6: Built-in Templates

- [ ] Create `templates/session-handoff.yaml` (lifecycle, from Phase 0)
- [ ] Create `templates/plan-execute.yaml` (phase-based)
- [ ] Create `templates/react.yaml` (phase-based)
- [ ] Create `templates/plan-act-reflect.yaml` (phase-based)
- [ ] Create `templates/plan-to-tasks.yaml` (phase-based, task decomposition)
- [ ] Create `templates/architect.yaml` (phase-based)
- [ ] Create `templates/test-driven.yaml` (phase-based)
- [ ] Install templates to `~/.gobby/workflows/templates/` on first run
- [ ] Enable `session-handoff` by default for all projects

### Phase 7: CLI Commands

- [ ] Implement `gobby workflow list`
- [ ] Implement `gobby workflow show <name>`
- [ ] Implement `gobby workflow set <name>`
- [ ] Implement `gobby workflow clear`
- [ ] Implement `gobby workflow status`
- [ ] Implement `gobby workflow phase <name>` (manual override)
- [ ] Implement `gobby workflow handoff <notes>`
- [ ] Implement `gobby workflow import <source>`

### Phase 8: MCP Tools

- [ ] Add `get_workflow_status` MCP tool
- [ ] Add `request_phase_transition` MCP tool
- [ ] Add `create_handoff` MCP tool
- [ ] Add `mark_artifact_complete` MCP tool
- [ ] Implement tool filtering based on workflow phase
- [ ] Update `list_tools` to respect phase restrictions

### Phase 9: Testing

- [ ] Unit tests for `WorkflowLoader` (YAML parsing)
- [ ] Unit tests for `WorkflowStateManager`
- [ ] Unit tests for condition evaluator
- [ ] Unit tests for `WorkflowEngine` phase transitions
- [ ] Integration tests for hook → workflow flow
- [ ] Integration tests for tool blocking
- [ ] Integration tests for context injection
- [ ] End-to-end test with plan-act-reflect workflow

### Phase 10: Documentation

- [ ] Document workflow YAML schema
- [ ] Document built-in templates
- [ ] Document CLI commands
- [ ] Document MCP tools
- [ ] Add examples for common patterns
- [ ] Update CLAUDE.md with workflow information

### Phase 11: Error Recovery Strategies

- [ ] Implement Daemon Crash Recovery (restore state from SQLite on restart)
- [ ] Implement Tool Timeout Handling (auto-transition to 'reflect' on persistent timeouts)
- [ ] Implement "Escape Hatch" commands (`--force`, `reset`, `disable`)

---

## Open Questions

1. **Workflow inheritance**: Should workflows be able to extend/override templates?

2. **Multi-workflow support**: Can a session have multiple active workflows (e.g., coding workflow + documentation workflow)?

3. **Cross-session state**: How long should workflow state persist? Per-session only, or carry across sessions?

4. **Approval UX**: How should user approvals work? Inject a question? Block until explicit command?

5. ~~**Escape hatches**: Should users be able to override workflow restrictions?~~ **Resolved:** Implemented via `--force`, `reset`, and `disable` CLI commands (see CLI Commands section)

6. **Workflow versioning**: How to handle workflow definition changes mid-session?

---

## Future Enhancements

- **Visual workflow builder**: Generate YAML from a visual diagram
- **Workflow analytics**: Track phase durations, transition patterns, common blocks
- **Shared workflows**: Publish/import workflows from a registry
- **AI-assisted workflow creation**: "Create a workflow for TDD" → generates YAML
- **Conditional tool arguments**: Not just block tools, but restrict arguments (e.g., only allow `pytest` not `rm`)
