# Orchestrator v3: Implementation Plan

> Final integrated plan. Supersedes `docs/research/orchestrator-v3-draft.md` (original 5-stage) and `docs/research/orchestrator-v3-p2-draft.md` (evolved 7-stage). Uses p2's structure with v3's implementation detail restored.

## Context

The current orchestrator creates one worktree per task, producing N branches and N merge operations for an epic with N subtasks. This compounds merge conflict risk, wastes disk/git overhead, and provides no intelligence about which tasks can safely run in parallel. The industry standard is worktree-per-agent with no shared-worktree parallel dispatch — we're building something novel with file-based dependency analysis.

Beyond the worktree problem, the expansion system is flaky — it misses requirements, gets dependencies wrong, and conflates research with task creation. Agent definitions use inheritance (`extends`) which is a poor abstraction for configuration. Agent definitions and their step workflows are split across two files for no good reason.

This plan addresses all of these and establishes a clean three-part mental model for the workflow system.

## Mental Model: Rules, Agents, Pipelines

After this work, the workflow system has three cleanly separated concerns:

**Rules** are reactive enforcement. They fire on events (before_tool, after_tool, session_start, stop) and apply effects (block, inject_context, set_variable, mcp_call). Stateless — they read session variables but don't own state. They define what you *can't* do.

> "Rules are guardrails. They react to events and enforce invariants. Block git push, require a task before editing, inject TDD instructions. They don't plan, they don't think — they enforce."

**Agents** are intelligent workers with phased behavior. An agent definition is: who you are (prompts) + what you do in what order (steps with constraints, gates, transitions). Steps define phases — each phase has tool restrictions, a goal, and a gate that advances to the next phase. The agent moves through its phases autonomously.

> "Agents are LLMs with a playbook. Each step says what tools you can use, what you're trying to accomplish, and what triggers moving to the next step. The developer agent claims a task, implements it, submits for review, then terminates — each phase enforced, each transition automatic."

**Pipelines** are deterministic orchestration. They sequence operations: MCP calls, shell commands, spawning agents. When they need intelligence, they spawn an agent. When they need mechanical work, they run MCP steps directly. Typed data flows between steps.

> "Pipelines are the assembly line. They coordinate who does what and in what order. They don't think — they dispatch. When a step needs reasoning, they spawn an agent. When it's mechanical, they call an MCP tool."

**How they compose:**

```text
Pipeline (orchestration)
  ├── mcp step: scan open tasks
  ├── mcp step: find next task
  ├── spawn agent: developer          ← agent does intelligent work
  │     ├── step: claim               ← phased constraints (locked down)
  │     ├── step: implement           ← creative freedom
  │     └── step: terminate           ← locked down
  │           (rules enforce invariants throughout)
  ├── spawn agent: qa-reviewer
  └── mcp step: merge
```

- **Pipelines** = how work is coordinated (deterministic)
- **Agents** = who does the work and how (phased, intelligent)
- **Rules** = what's enforced everywhere (reactive guardrails)

## Resolved Design Decisions

| Question | Resolution |
|----------|-----------|
| Q1: Agent interaction model | Agents call MCP tools directly (`add_dependency`, `set_affected_files`) |
| Q2: Planning agent vs expansion | Expansion sub-pipeline replaces both the current skill AND the v2 planning agent concept |
| Q3: Formatter serialization | Orchestrator runs format step between parallel batches; dependency agent identifies serialization points |
| Q4: Unexpected file touches | Accept imperfection. Post-hoc update via `annotation_source='observed'` from git diff. No mid-execution re-analysis |
| Q5: Clones (Gemini) | Orchestrator is isolation-agnostic. `spawn_agent` already supports both `worktree_id` and `clone_id` |
| Q6: Staging | 7 stages (reordered from original 5, new stages added) |
| Q7: Agent inheritance | Scrap `extends`. Agents are self-contained, compose via selectors |
| Q8: Inline rule_definitions | Removed from agents. All rules are templates |
| Q9: Agent = step workflow | Agents absorb their step workflow definitions. One file, one concept |
| Q10: Pipeline auto-run | session_start rule locks agent down to run_pipeline |
| Q11: Expansion approach | Hard boundary: research agent produces spec, mechanical builder creates tasks. No mixing |

---

## Stage 1: Single Worktree Per Epic + Bug Fixes

**Goal**: Stop creating N branches/worktrees. One worktree per epic, sequential dispatch. Fix real bugs found during exploration.

### 1a. Fix orchestrator pipeline — explicit `use_local` and `provider`

**File**: `src/gobby/install/shared/workflows/orchestrator.yaml`

- Modify `create_worktree` step to pass `use_local: true` explicitly (the auto-detection exists in the MCP tool but the pipeline should be explicit)
- Pass `provider` from pipeline input so CLI hooks get installed in the worktree
- Bug #9538 note: the MCP tool itself is fine — it auto-detects and passes `use_local`. The issue is pipeline-level, not tool-level

### 1b. Restructure pipeline: one worktree per epic

**File**: `src/gobby/install/shared/workflows/orchestrator.yaml`

Current flow: `suggest_next_task` → `create_worktree` (per task) → `spawn_developer`
New flow:
- **First iteration only**: `create_worktree` with epic-level branch name, store `_worktree_id` in pipeline state
- **Subsequent iterations**: Reuse `_worktree_id` (already supported by `spawn_agent`)
- **Merge phase**: One merge at epic completion (epic branch → target), not per-task
- Remove per-task worktree creation step
- QA agent gets `worktree_id` so it can see the actual changes

### 1c. Pass worktree context to QA agent

**File**: `src/gobby/install/shared/workflows/orchestrator.yaml`

- Current `spawn_qa` passes no `worktree_id` — QA can't see worktree code
- Pass `_worktree_id` to QA spawn step

### 1d. Add `use_local` support to clones

**File**: `src/gobby/clones/git.py` (method `create_clone`, ~line 509)

Clones always clone from the remote URL (`get_remote_url()`), missing unpushed local commits. Fix: when `use_local=True`, clone from the local repo path instead of the remote URL.

```python
def create_clone(
    self,
    clone_path: str | Path,
    branch_name: str,
    base_branch: str = "main",
    shallow: bool = True,
    use_local: bool = False,      # NEW parameter
) -> GitOperationResult:
```

When `use_local=True`:
- Use `str(self.repo_path)` as the clone source instead of `get_remote_url()`
- Full clone (not shallow) — shallow clones from local paths can be unreliable
- This gives the clone all local commits including unpushed work

Wire `use_local` through:
- `src/gobby/mcp_proxy/tools/clones.py` — add `use_local` param to `create_clone` MCP tool (mirror worktree tool pattern)
- `src/gobby/agents/isolation.py` — `CloneIsolationHandler.prepare_environment` should auto-detect unpushed commits (same as `WorktreeIsolationHandler` already does)

### Verification

- Run orchestrator pipeline on a test epic with 3+ subtasks
- Confirm only 1 worktree/branch is created
- Confirm all agents work in the same worktree
- Confirm QA agent can see code changes
- Confirm merge produces a single merge operation
- Create a clone with `use_local=True` from a branch with unpushed commits — confirm clone has them
- Create a clone with `use_local=False` — confirm existing behavior unchanged

---

## Stage 2: Agent System Overhaul

**Goal**: Simplify agent definitions. Agents are self-contained — no inheritance, no separate step workflow files, no inline rule definitions.

### 2a. Scrap `extends` on agent definitions

**Files**:
- `src/gobby/workflows/definitions.py` — remove `extends` field from `AgentDefinitionBody`
- `src/gobby/workflows/agent_resolver.py` — replace merge logic with direct lookup
- `src/gobby/install/shared/agents/*.yaml` — update all agents to be self-contained

**What changes:**
- Remove `extends` field from `AgentDefinitionBody`
- `resolve_agent()` becomes a simple DB lookup (no chain resolution, no merging)
- Remove `_merge_agent_bodies()` entirely
- Each agent declares its own `rule_selectors`, `variables`, prompts — no inheritance
- Agents that currently extend `default` get the relevant fields inlined

**Migration**: For each agent that uses `extends: default`:
- Copy any needed prompt fields from default
- Declare `rule_selectors: { include: ["tag:gobby"] }` explicitly
- Set `provider` explicitly (or keep `"inherit"` for spawn-time resolution)

### 2b. Remove inline `rule_definitions` from agent definitions

**Files**:
- `src/gobby/workflows/definitions.py` — remove `rule_definitions` field from `AgentDefinitionBody`
- `src/gobby/install/shared/agents/*.yaml` — extract inline rules to templates
- `src/gobby/install/shared/rules/` — new rule templates for extracted rules

**What changes:**
- Remove `rule_definitions: dict[str, RuleDefinition]` from `AgentDefinitionBody`
- Developer's `no_push` becomes a standalone rule template with `agent_scope: [developer]`
- Agents reference rules via `rule_selectors` only — one mechanism

### 2c. Agents absorb their step workflows

**Files**:
- `src/gobby/workflows/definitions.py` — add `steps` field to `AgentDefinitionBody` (reuse existing `WorkflowStep` model)
- `src/gobby/install/shared/agents/developer.yaml` — absorb `developer-workflow.yaml` steps
- `src/gobby/install/shared/workflows/developer-workflow.yaml` — remove (absorbed into agent)
- `src/gobby/workflows/loader.py` — when loading an agent, auto-register its steps as a step workflow
- Repeat for any other agent + step workflow pairs

**Developer agent after merge:**

```yaml
name: developer
description: "Developer agent: implements tasks, writes tests, commits, marks for review."
version: "2.0"
enabled: false

instructions: |
  You are a developer agent spawned by an orchestrator pipeline.
  Your task ID is in your session variables as assigned_task_id.
  Follow the step prompts — your behavior is enforced per-phase.

workflows:
  rule_selectors:
    include: ["tag:gobby", "agent:developer"]
  variables:
    task_claimed: false
    review_submitted: false

steps:
  - name: claim
    description: "Claim the assigned task"
    status_message: "Claim your task by calling claim_task, then get_task for details."
    allowed_tools:
      - mcp__gobby__call_tool
      - mcp__gobby__list_mcp_servers
      - mcp__gobby__list_tools
      - mcp__gobby__get_tool_schema
    allowed_mcp_tools:
      - "gobby-tasks:claim_task"
      - "gobby-tasks:get_task"
    on_mcp_success:
      - server: gobby-tasks
        tool: claim_task
        action: set_variable
        variable: task_claimed
        value: true
    transitions:
      - to: implement
        when: "vars.task_claimed"

  - name: implement
    description: "Read code, implement changes, write tests, run tests, commit"
    status_message: "Implement the task. Commit, then call mark_task_needs_review."
    allowed_tools: "all"
    blocked_mcp_tools:
      - "gobby-tasks:close_task"
      - "gobby-tasks:mark_task_review_approved"
      - "gobby-agents:spawn_agent"
      - "gobby-agents:kill_agent"
    on_mcp_success:
      - server: gobby-tasks
        tool: mark_task_needs_review
        action: set_variable
        variable: review_submitted
        value: true
    transitions:
      - to: terminate
        when: "vars.review_submitted"

  - name: terminate
    description: "Self-terminate"
    status_message: "Call kill_agent to terminate."
    allowed_tools:
      - mcp__gobby__call_tool
      - mcp__gobby__list_mcp_servers
      - mcp__gobby__list_tools
      - mcp__gobby__get_tool_schema
    allowed_mcp_tools:
      - "gobby-agents:kill_agent"
```

**Implementation**: When an agent with `steps` is spawned, the loader auto-registers a step workflow from the agent's steps (using the existing `WorkflowDefinition` model + `WorkflowStep` model). The rule engine processes it exactly as before — no changes to step workflow enforcement logic.

### 2d. Update agent definition MCP tools

**File**: `src/gobby/mcp_proxy/tools/agent_definitions.py`

- `_agent_detail()` — add `"steps": body.get("steps")` to output
- `_agent_summary()` — add `"has_steps": bool(body.get("steps"))` for list view
- Add `update_agent_steps(name, steps)` — follows `update_agent_rules` pattern, replaces entire steps list
- `get_agent_definition` — remove `resolve_agent` call (no more extends), simplify to direct lookup
- `create_agent_definition` — steps flow through automatically via `AgentDefinitionBody` validation

### 2e. Agent step editor UI

**Files**: `web/src/` — agent definition editor components

The UI needs to support viewing and editing agent steps as a first-class concept:
- **Step list view**: Show agent's steps in order with name, description, constraint summary
- **Step editor**: Edit per-step fields — allowed_tools, blocked_mcp_tools, status_message, transitions, on_mcp_success handlers
- **Step reordering**: Drag or move steps up/down
- **Transition visualization**: Show step flow (claim → implement → terminate) as a simple diagram or flow indicator
- **Gate editor**: Configure what triggers step advancement (MCP success handlers, variable conditions)

Design should follow existing agent definition editor patterns in the UI.

### 2f. Auto-run pipeline via session_start rule

**Files**:
- `src/gobby/install/shared/rules/pipeline-enforcement/auto-run-pipeline.yaml` — new rule
- Deprecate: `inject-pipeline-instructions.yaml`, `enforce-pipeline-tools.yaml`, `restrict-pipeline-call-tool.yaml`

**New rule:**

```yaml
rules:
  auto-run-pipeline:
    description: "Lock down agent to run its assigned pipeline on session start"
    event: session_start
    enabled: false
    when: "variables.get('_assigned_pipeline')"
    effects:
      - type: inject_context
        template: |
          Run your assigned pipeline: {{ _assigned_pipeline }}
          Use progressive disclosure to call run_pipeline.
```

Combined with existing tool restrictions for pipeline agents (agent has no steps defined, so tool restrictions come from the lockdown rules). Replaces three separate rules with one.

### Verification

- Developer agent definition includes steps — spawned agent progresses through claim → implement → terminate
- Extends removed — agents that previously inherited work standalone
- Inline rule_definitions removed — rules extracted to templates
- Step workflow enforcement unchanged (rule engine still processes steps)
- Pipeline auto-run works for pipeline-attached agents
- MCP tools: `get_agent_definition` returns steps, `create_agent_definition` accepts steps, `update_agent_steps` works
- UI: Agent editor shows steps, allows editing constraints/transitions/gates

---

## Stage 3: `task_affected_files` Infrastructure

**Goal**: Build the data layer for file-based dependency analysis.

### 3a. Database migration — `task_affected_files` table

**File**: `src/gobby/storage/migrations.py`

```sql
CREATE TABLE task_affected_files (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    annotation_source TEXT NOT NULL DEFAULT 'expansion',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(task_id, file_path)
);
CREATE INDEX idx_taf_task_id ON task_affected_files(task_id);
CREATE INDEX idx_taf_file_path ON task_affected_files(file_path);
```

- Add as next migration number (check current `BASELINE_VERSION`, currently 133)
- Update `BASELINE_SCHEMA` with the new table
- `annotation_source`: `'expansion'` | `'manual'` | `'observed'`

### 3b. `TaskAffectedFileManager`

**New file**: `src/gobby/storage/task_affected_files.py`

Follow `TaskDependencyManager` pattern (`src/gobby/storage/task_dependencies.py`):

```python
class TaskAffectedFileManager:
    def __init__(self, db: DatabaseProtocol)
    def set_files(self, task_id: str, files: list[str], source: str = "expansion") -> None
    def get_files(self, task_id: str) -> list[TaskAffectedFile]
    def add_file(self, task_id: str, file_path: str, source: str) -> TaskAffectedFile
    def remove_file(self, task_id: str, file_path: str) -> bool
    def find_overlapping_tasks(self, task_ids: list[str]) -> dict[tuple[str, str], list[str]]
    def get_tasks_for_file(self, file_path: str) -> list[TaskAffectedFile]
```

Key method: `find_overlapping_tasks` — given a set of task IDs, returns `{(task_a, task_b): [shared_file_paths]}` for all pairs with file overlap.

### 3c. Expansion prompt — add `affected_files` and `parallel_group`

**File**: `src/gobby/tasks/prompts/expand-task.md`

Add to the subtask schema table:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `affected_files` | `array[string]` | No | Relative file paths this subtask will read or modify |
| `parallel_group` | `string` | No | Group label for tasks safe to run concurrently |

Add guidance in the prompt about predicting file paths and grouping parallel work.

### 3d. Tree builder — wire `affected_files`

**File**: `src/gobby/tasks/tree_builder.py`

In `_create_node()`, after `task_manager.create_task(...)`:
- Read `node.get("affected_files", [])`
- Call `TaskAffectedFileManager.set_files(task.id, affected_files)`
- ~5-10 lines of new code

Store `parallel_group` as a task label with `parallel:` prefix (e.g., `parallel:group-a`). Tasks have `labels: list[str]` as a JSON column — no schema change needed. Query with `json_each(labels)` in SQLite.

### 3e. MCP tools for affected files

**New file**: `src/gobby/mcp_proxy/tools/tasks/_affected_files.py` (follows `_crud.py` pattern in tasks tools directory)

- `set_affected_files(task_id, files, source)` — for agents to call directly
- `get_affected_files(task_id)` — query
- `find_file_overlaps(task_ids)` — contention detection

Register in `src/gobby/mcp_proxy/tools/tasks/__init__.py` alongside existing task tool registries.

### Verification

- Unit tests for `TaskAffectedFileManager` (CRUD, overlap detection)
- Expand a test task and verify `affected_files` are stored
- Query overlaps between tasks and verify correct pairs returned
- Migration applies cleanly on existing databases

---

## Stage 4: Expansion Sub-Pipeline

**Goal**: Replace the flaky expansion skill with a deterministic pipeline. Hard boundary between research (creative) and task creation (mechanical).

### Design Principles

1. **Separation of concerns**: Research agent produces a spec. Mechanical builder creates tasks. No mixing.
2. **Spec is the contract**: Once finalized, the builder translates it faithfully — no invention.
3. **Reusable**: Orchestrator invokes it, `/gobby expand` wraps it, other pipelines compose with it.
4. **Validation catches missed requirements**: Mechanical check that all plan sections are covered.

### 4a. Expansion agent definition

**New file**: `src/gobby/install/shared/agents/expander.yaml`

Agent with steps:
- Step 1 (`research`): Full tool access except `execute_expansion`. Explores codebase, produces spec. Gate: `save_expansion_spec` called.
- Step 2 (`complete`): Locked down to `kill_agent`. Gate: `kill_agent` called.

The agent's job is ONLY to produce a good spec. It does not create tasks.

### 4b. Expansion pipeline definition

**New file**: `src/gobby/install/shared/workflows/expand-task.yaml`

```yaml
name: expand-task
type: pipeline
description: "Reusable expansion sub-pipeline: agent research → validate → execute"

inputs:
  task_id: null
  session_id: null
  plan_content: null

steps:
  - id: spawn_researcher
    mcp:
      server: gobby-agents
      tool: spawn_agent
      arguments:
        agent: expander
        task_id: "${{ inputs.task_id }}"
        prompt: |
          Research and produce an expansion spec for task ${{ inputs.task_id }}.
          ${{ inputs.plan_content if inputs.plan_content else '' }}
          Save your spec via save_expansion_spec. Do NOT invent requirements.
        mode: terminal

  - id: wait_for_researcher
    mcp:
      server: gobby-agents
      tool: wait_for_agent
      arguments:
        run_id: "${{ spawn_researcher.output.run_id }}"

  - id: validate
    mcp:
      server: gobby-tasks
      tool: validate_expansion_spec
      arguments:
        task_id: "${{ inputs.task_id }}"

  - id: execute
    mcp:
      server: gobby-tasks
      tool: execute_expansion
      arguments:
        parent_task_id: "${{ inputs.task_id }}"
        session_id: "${{ inputs.session_id }}"

  - id: wire_affected_files
    condition: "${{ execute.output.subtask_ids is defined }}"
    mcp:
      server: gobby-tasks
      tool: wire_affected_files_from_spec
      arguments:
        parent_task_id: "${{ inputs.task_id }}"

  - id: analyze_dependencies
    condition: "${{ execute.output.subtask_ids is defined }}"
    mcp:
      server: gobby-tasks
      tool: find_file_overlaps
      arguments:
        task_ids: "${{ execute.output.subtask_ids }}"
```

### 4c. `validate_expansion_spec` MCP tool

**New addition to**: `src/gobby/mcp_proxy/tools/tasks/_expansion.py`

Validates a saved spec:
- Non-empty subtasks list
- Valid dependency indices (no out-of-bounds, no self-references, no cycles)
- Required fields present (title, description, category)
- If source plan has numbered sections, verify coverage
- Returns `{valid: bool, errors: list[str]}`

### 4d. `wire_affected_files_from_spec` MCP tool

**New addition to**: `src/gobby/mcp_proxy/tools/tasks/_affected_files.py`

Reads spec from parent task, extracts `affected_files` per subtask, stores via `TaskAffectedFileManager`.

### 4e. Update `/gobby expand` skill to delegate

**File**: `src/gobby/install/shared/skills/expand/SKILL.md`

Thin wrapper: validate input → invoke `expand-task` pipeline → report results.

### 4f. Wire into orchestrator pipeline

**File**: `src/gobby/install/shared/workflows/orchestrator.yaml`

Add `invoke_pipeline: expand-task` before dispatch if epic not yet expanded.

### Verification

- Expand via pipeline → spec saved, validated, executed
- All plan sections covered (no missed requirements)
- `/gobby expand` delegates to pipeline
- Orchestrator invokes expansion before dispatch

---

## Stage 5: Parallel Dispatch

**Goal**: `suggest_next_tasks` (plural) returns batches of non-conflicting tasks. Orchestrator dispatches multiple agents into the shared worktree simultaneously.

### 5a. `suggest_next_tasks` MCP tool

**File**: `src/gobby/mcp_proxy/tools/task_readiness.py`

- Extract shared scoring logic from existing `suggest_next_task` into helper
- New tool returns batch of non-conflicting ready tasks
- Uses `TaskAffectedFileManager.find_overlapping_tasks()` to filter
- Considers in-progress tasks' file annotations (don't dispatch overlapping work)
- Greedy selection: iterate by score, add task if no file conflict with already-selected set
- Return shape: `{"suggestions": [...], "total_ready": int}`

### 5b. Orchestrator parallel dispatch

**File**: `src/gobby/install/shared/workflows/orchestrator.yaml`

- Replace `suggest_next_task` with `suggest_next_tasks`
- Dispatch multiple `spawn_developer` calls per iteration (fire-and-forget)
- Add format step between batches (orchestrator-owned serialization point)
- `max_concurrent` input caps parallel agent count

### 5c. Post-hoc file annotation update

After each task completes, update `task_affected_files` with `annotation_source='observed'` from `git diff`. This improves future predictions and catches unexpected file touches.

### Verification

- Orchestrator dispatches 2+ agents in parallel for non-conflicting tasks
- Conflicting tasks are serialized (not dispatched together)
- Format step runs between batches
- Post-hoc annotations update after task completion

---

## Stage 6: Deterministic TDD Enforcement

**Goal**: Replace prompt-based TDD expansion with deterministic rule enforcement. Agents are blocked from writing implementation code until tests exist, and validation criteria are injected automatically per-file to enforce the outcome. Independent of Stages 2-5.

### Design

Two-layer enforcement:
- **Process layer** (PreToolUse rule): One-shot nudge — blocks the first Write to a new code file, tells the agent to write a test first
- **Outcome layer** (validation criteria): Per-file criteria injected via `mcp_call` to `update_task` — task can't close without tests for each file

### 6a. Add Jinja2 rendering to `mcp_call` arguments

**File**: `src/gobby/workflows/rule_engine.py` (method `_apply_non_block_effect`, ~line 420)

Currently `mcp_call` arguments are passed through raw. Block `reason` and `inject_context` templates are Jinja2-rendered. This is inconsistent. Add rendering for string values in `effect.arguments`:

```python
elif effect.type == "mcp_call":
    # Render string argument values through Jinja2 (consistent with block/inject)
    rendered_args = {}
    for k, v in (effect.arguments or {}).items():
        if isinstance(v, str) and "{{" in v:
            rendered_args[k] = self._render_template(v, ctx, allowed_funcs)
        else:
            rendered_args[k] = v
    mcp_calls.append({
        "server": effect.server,
        "tool": effect.tool,
        "arguments": rendered_args,
        "background": effect.background,
    })
```

~10 lines changed. Enables dynamic argument values in all `mcp_call` effects, not just TDD.

### 6b. `enforce_tdd` variable definitions

**File**: `src/gobby/install/shared/variables/gobby-default-variables.yaml`

Add alongside existing variables:

```yaml
  enforce_tdd:
    value: false
    description: "Enable TDD enforcement — block implementation writes until tests exist"
  tdd_nudged_files:
    value: []
    description: "Tracks files that have been TDD-nudged (internal, do not set manually)"
  tdd_tests_written:
    value: []
    description: "Tracks test files written during TDD (internal, do not set manually)"
```

### 6c. Rule: `enforce-tdd-block` (before_tool)

**File**: `src/gobby/install/shared/rules/enforce-tdd-block.yaml`

```yaml
tags: [tdd, enforcement]

rules:
  enforce-tdd-block:
    description: "Block writes to new code files until a test is written first"
    event: before_tool
    enabled: false
    priority: 35
    when: >
      variables.get('enforce_tdd')
      and tool_input.get('file_path', '') not in variables.get('tdd_nudged_files', [])
      and not tool_input.get('file_path', '').endswith(('.yaml', '.yml', '.json', '.toml', '.md', '.txt', '.cfg', '.ini', '.lock'))
      and not tool_input.get('file_path', '').endswith('__init__.py')
      and not tool_input.get('file_path', '').startswith('tests/')
      and 'test_' not in tool_input.get('file_path', '').split('/')[-1]
      and '_test.' not in tool_input.get('file_path', '')
      and '.test.' not in tool_input.get('file_path', '')
      and '_spec.' not in tool_input.get('file_path', '')
      and '.spec.' not in tool_input.get('file_path', '')
    effects:
      - type: set_variable
        variable: tdd_nudged_files
        value: "variables.get('tdd_nudged_files', []) + [tool_input.get('file_path', '')]"
      - type: mcp_call
        server: gobby-tasks
        tool: update_task
        arguments:
          task_id: "{{ claimed_task_id }}"
          validation_criteria: "{{ 'Tests required for:\\n' + (variables.get('tdd_nudged_files', []) | join('\\n')) }}"
      - type: block
        tools: [Write]
        reason: >
          TDD enforcement: write a test for `{{ tool_input.get('file_path', '') }}` before
          writing the implementation. Create a failing test first, then implement to make it pass.
```

**How it works — step-by-step walkthrough:**
1. Agent tries to Write `src/gobby/foo/bar.py`
2. Rule checks: `enforce_tdd` is true, file is a code file, not a test, not yet nudged
3. Effects fire in order (non-block effects first, block deferred):
   - `set_variable`: adds `src/gobby/foo/bar.py` to `tdd_nudged_files` list (one-shot)
   - `mcp_call`: calls `update_task` with `validation_criteria` rendered from the full `tdd_nudged_files` list (now includes current file). Jinja2 rendering enabled by 6a.
   - `block`: prevents the Write, tells agent to write test first
4. Agent writes `tests/foo/test_bar.py` — rule doesn't fire (test file, excluded by conditions)
5. Agent retries `src/gobby/foo/bar.py` — rule doesn't fire (file already in `tdd_nudged_files`)
6. At close time, `TaskValidator` checks `validation_criteria` which now lists every implementation file that needs tests

**Note on set_variable + mcp_call ordering**: `set_variable` fires before `mcp_call` (both are non-block effects, processed in declaration order). So when the `mcp_call` renders `tdd_nudged_files`, it already includes the current file path.

### 6d. Rule: `enforce-tdd-track-tests` (after_tool)

**File**: `src/gobby/install/shared/rules/enforce-tdd-track-tests.yaml`

```yaml
tags: [tdd, observability]

rules:
  enforce-tdd-track-tests:
    description: "Track test files written for TDD observability"
    event: after_tool
    enabled: false
    priority: 35
    when: >
      variables.get('enforce_tdd')
      and not event.data.get('error')
      and (tool_input.get('file_path', '').startswith('tests/')
           or 'test_' in tool_input.get('file_path', '').split('/')[-1]
           or '_test.' in tool_input.get('file_path', '')
           or '.test.' in tool_input.get('file_path', '')
           or '_spec.' in tool_input.get('file_path', '')
           or '.spec.' in tool_input.get('file_path', ''))
    effect:
      type: set_variable
      variable: tdd_tests_written
      value: "variables.get('tdd_tests_written', []) + [tool_input.get('file_path', '')]"
```

Tracks which test files were written. Useful for observability/metrics — e.g., "agent wrote 3 tests before 5 implementation files."

### Key Design Choices

| Choice | Rationale |
|--------|-----------|
| Write only, not Edit | Write creates new files. Edit modifies existing. TDD targets new code, not modifications |
| One-shot nudge via `tdd_nudged_files` | No complex state machine. Block once, let through after. Real enforcement at validation |
| `mcp_call` with Jinja2 rendering | Deterministic per-file criteria on the task, not prompt-based. Requires 6a (small engine change) |
| Per-file validation_criteria | Each blocked file is listed. Validator has exact file list to check against git diff |
| Config/init files excluded | `__init__.py`, `.yaml`, `.json`, `.md` etc. aren't TDD targets |
| Test detection via filename patterns | `tests/`, `test_*`, `*_test.*`, `*.test.*`, `*_spec.*`, `*.spec.*` — covers Python, JS/TS, Go |
| Default `false` | Opt-in. Not everyone wants TDD enforcement |
| `parallel_group` stored as label | Tasks have `labels: list[str]` (JSON column). Use `parallel:group-name` prefix. No schema change |

### Verification

- Enable `enforce_tdd = true` on a session
- Attempt to Write a new `.py` file — confirm block fires with TDD message
- Confirm `tdd_nudged_files` variable contains the file path
- Confirm `update_task` was called with per-file validation_criteria
- Write a test file — confirm no block, confirm `tdd_tests_written` updated
- Retry the `.py` file — confirm it goes through (already nudged)
- Close task — confirm validation checks the per-file test requirements
- Confirm config/init/`.md` files are not blocked
- Confirm Edit tool is not blocked (existing file modifications)
- Test mcp_call Jinja2 rendering works for other use cases too

---

## Stage 7: Documentation (Full Rewrite)

**Goal**: Delete existing workflow guides and write comprehensive documentation from scratch for the three-part model.

### 7a. Delete existing guides

**Delete all of these:**
- `docs/guides/workflows.md`
- `docs/guides/workflow-rules.md`
- `docs/guides/workflow-actions.md`
- `docs/guides/agent_definitions.md`
- `docs/guides/pipelines.md`
- `docs/guides/orchestration.md`

These are outdated and reference removed concepts (extends, inline rule_definitions, separate step workflows). Clean slate.

### 7b. Workflow system overview (new)

**New file**: `docs/guides/workflows.md`

- The three-part mental model: rules, agents, pipelines
- How they compose (with visual diagram)
- When to use each
- Glossary of terms (step, gate, transition, effect, rule selector, etc.)

### 7c. Rules guide (new)

**New file**: `docs/guides/rules.md`

- What rules are and when to use them
- Events: before_tool, after_tool, session_start, stop
- Effects: block, inject_context, set_variable, mcp_call, observe
- Conditions: `when` expressions, available context variables
- Scoping: `agent_scope`, tags, `rule_selectors`
- Jinja2 rendering in templates and mcp_call arguments
- Complete examples for common patterns (block tool, inject context, track state, TDD enforcement)
- YAML schema reference

### 7d. Agents guide (new)

**New file**: `docs/guides/agents.md`

- What agents are: intelligent workers with phased behavior
- Agent = prompts + steps + rule selectors
- Step model: phases, constraints (allowed_tools, blocked_mcp_tools), gates (on_mcp_success), transitions
- How to write an agent definition from scratch
- Composition via rule selectors (no extends, no inline rules)
- Agent spawning: isolation modes (none, worktree, clone), execution modes (terminal, autonomous, self), providers
- Pipeline attachment: `workflows.pipeline` field, auto-run behavior
- Complete example: developer agent (full YAML with steps)
- Complete example: expansion agent
- YAML schema reference

### 7e. Pipelines guide (new)

**New file**: `docs/guides/pipelines.md`

- What pipelines are: deterministic orchestration sequences
- Step types: exec, mcp, prompt, invoke_pipeline
- Data flow: `${{ inputs.foo }}`, `${{ steps.step_id.output }}`
- Spawning agents from pipeline steps
- Sub-pipelines via invoke_pipeline (cycle detection, depth limit)
- Approval gates
- Conditions: `${{ ... }}` gating
- Complete example: expansion pipeline
- Complete example: orchestrator pipeline
- YAML schema reference

### 7f. Orchestration guide (new)

**New file**: `docs/guides/orchestration.md`

- How the orchestrator pipeline works (v3)
- Epic lifecycle: expand → dispatch → QA → merge
- Single worktree per epic
- Parallel dispatch with file-overlap awareness
- Agent roles: expander, developer, QA, merge
- Configuration inputs and customization

### Verification

- All guides written from scratch — no leftover references to removed concepts
- Each guide is self-contained with complete examples
- YAML schema references match actual implementation
- Examples are testable against running system

---

## Implementation Order

```text
Stage 1 ──────────────────────────────► (immediate value, bug fixes)
Stage 2 ──────────────────────────────► (foundational: agent simplification)
         ├─ Stage 3 ──────────────────► (data layer, parallel with 6)
         ├─ Stage 4 ──────────────────► (expansion pipeline)
         │   └─ Stage 5 ──────────────► (needs 3 + 4)
         ├─ Stage 6 ──────────────────► (independent, parallel with 3-5)
         └─ Stage 7 ──────────────────► (docs, after all code stages)
```

Stage 1 first — immediate value. Stage 2 next — foundational. Stages 3 and 6 can run in parallel. Stage 4 depends on Stage 2 (agents with steps) + benefits from Stage 3 (affected_files). Stage 5 depends on 3 + 4. Stage 7 last — documents the final state. All stages are independently shippable except Stage 5.

---

## Critical Files

| File | Stage | Action |
|------|-------|--------|
| `src/gobby/install/shared/workflows/orchestrator.yaml` | 1, 4, 5 | Restructure pipeline, wire expansion, parallel dispatch |
| `src/gobby/clones/git.py` | 1 | Add `use_local` param to `create_clone` |
| `src/gobby/mcp_proxy/tools/clones.py` | 1 | Wire `use_local` to MCP tool |
| `src/gobby/agents/isolation.py` | 1 | Auto-detect unpushed commits in `CloneIsolationHandler` |
| `src/gobby/workflows/definitions.py` | 2 | Remove `extends`, `rule_definitions`; add `steps` to `AgentDefinitionBody` |
| `src/gobby/workflows/agent_resolver.py` | 2 | Simplify to direct lookup (no merge logic) |
| `src/gobby/workflows/loader.py` | 2 | Auto-register agent steps as step workflow |
| `src/gobby/install/shared/agents/*.yaml` | 2 | Migrate all agents to composition + absorb steps |
| `src/gobby/mcp_proxy/tools/agent_definitions.py` | 2 | Add steps to detail/summary, add `update_agent_steps` |
| `web/src/` (agent editor components) | 2 | Step editor UI for agent definitions |
| `src/gobby/install/shared/rules/pipeline-enforcement/` | 2 | Consolidate to `auto-run-pipeline` rule |
| `src/gobby/storage/migrations.py` | 3 | Add migration for `task_affected_files` table |
| `src/gobby/storage/task_affected_files.py` | 3 | New manager (follows `task_dependencies.py` pattern) |
| `src/gobby/tasks/prompts/expand-task.md` | 3 | Add `affected_files` + `parallel_group` fields |
| `src/gobby/tasks/tree_builder.py` | 3 | Wire `affected_files` into `_create_node` |
| `src/gobby/mcp_proxy/tools/tasks/_affected_files.py` | 3, 4 | New MCP tools for affected files |
| `src/gobby/install/shared/agents/expander.yaml` | 4 | New expansion agent definition |
| `src/gobby/install/shared/workflows/expand-task.yaml` | 4 | New expansion pipeline |
| `src/gobby/mcp_proxy/tools/tasks/_expansion.py` | 4 | Add `validate_expansion_spec` |
| `src/gobby/install/shared/skills/expand/SKILL.md` | 4 | Thin wrapper over pipeline |
| `src/gobby/mcp_proxy/tools/task_readiness.py` | 5 | `suggest_next_tasks` (plural) |
| `src/gobby/workflows/rule_engine.py` | 6 | Add Jinja2 rendering to `mcp_call` arguments |
| `src/gobby/install/shared/variables/gobby-default-variables.yaml` | 6 | Add `enforce_tdd` + tracking variables |
| `src/gobby/install/shared/rules/enforce-tdd-block.yaml` | 6 | New rule template |
| `src/gobby/install/shared/rules/enforce-tdd-track-tests.yaml` | 6 | New rule template |
| `docs/guides/workflows.md` | 7 | Delete and rewrite — three-part model overview |
| `docs/guides/workflow-rules.md` | 7 | Delete |
| `docs/guides/workflow-actions.md` | 7 | Delete |
| `docs/guides/agent_definitions.md` | 7 | Delete |
| `docs/guides/pipelines.md` | 7 | Delete and rewrite |
| `docs/guides/orchestration.md` | 7 | Delete and rewrite |
| `docs/guides/rules.md` | 7 | New — rules guide |
| `docs/guides/agents.md` | 7 | New — agents guide |

## Existing Code to Reuse

| What | Where | Stage | How |
|------|-------|-------|-----|
| `TaskDependencyManager` pattern | `src/gobby/storage/task_dependencies.py` | 3 | Mirror for `TaskAffectedFileManager` |
| `suggest_next_task` scoring | `src/gobby/mcp_proxy/tools/task_readiness.py` | 5 | Extract into shared helper for `suggest_next_tasks` |
| `TaskTreeBuilder._create_node` | `src/gobby/tasks/tree_builder.py` | 3 | Add `affected_files` read after task creation |
| Worktree reuse in `spawn_agent` | `src/gobby/mcp_proxy/tools/spawn_agent/_implementation.py` | 1 | Already works — just pass `worktree_id` |
| `WorktreeGitManager.create_worktree` | `src/gobby/worktrees/git.py` | 1 | `use_local` fully implemented |
| Migration pattern | `src/gobby/storage/migrations.py` | 3 | Add numbered migration + update baseline |
| Rule template patterns | `src/gobby/install/shared/rules/require-task-before-edit.yaml` | 6 | PreToolUse block + `tool_input.get('file_path')` pattern |
| Expression-based `set_variable` | `src/gobby/install/shared/rules/track-listed-servers.yaml` | 6 | List append via `variables.get(..., []) + [...]` |
| `AgentDefinitionBody` model | `src/gobby/workflows/definitions.py` | 2 | Add `steps` field, remove `extends` + `rule_definitions` |
| `WorkflowStep` model | `src/gobby/workflows/definitions.py` | 2 | Reuse for agent-embedded steps |
| Step workflow loading | `src/gobby/workflows/loader.py` | 2 | Auto-register agent steps as step workflow |
| `save_expansion_spec` / `execute_expansion` | `src/gobby/mcp_proxy/tools/tasks/_expansion.py` | 4 | Add `validate_expansion_spec` alongside |
| Existing doc guides structure | `docs/guides/` | 7 | Delete and replace with three-part model docs |
