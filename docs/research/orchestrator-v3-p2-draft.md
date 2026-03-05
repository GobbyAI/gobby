# Orchestrator v3: Updated Implementation Plan (DRAFT — SUPERSEDED)

> **Superseded by `docs/plans/orchestrator-v3-final.md`.** This was the proposed replacement for the original v3 draft. Both drafts have been integrated into the final plan.

## Context

The current orchestrator creates one worktree per task, producing N branches and N merge operations for an epic with N subtasks. The expansion system is flaky — it misses requirements, gets dependencies wrong, and conflates research with task creation. Agent definitions use inheritance (`extends`) which is a poor abstraction for configuration. Agent definitions and their step workflows are split across two files for no good reason. This plan addresses all of these and establishes a clean three-part mental model for the workflow system.

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

### Resolved Design Decisions

| Question | Resolution |
|----------|-----------|
| Q1: Agent interaction model | Agents call MCP tools directly |
| Q2: Planning agent vs expansion | Expansion sub-pipeline replaces both the current skill AND the v2 planning agent concept |
| Q3: Formatter serialization | Orchestrator runs format step between parallel batches |
| Q4: Unexpected file touches | Post-hoc update via `annotation_source='observed'` from git diff |
| Q5: Clones (Gemini) | Orchestrator is isolation-agnostic |
| Q6: Staging | 7 stages (reordered, new stages added) |
| Q7: Agent inheritance | Scrap `extends`. Agents are self-contained, compose via selectors |
| Q8: Inline rule_definitions | Removed from agents. All rules are templates |
| Q9: Agent = step workflow | Agents absorb their step workflow definitions. One file, one concept |
| Q10: Pipeline auto-run | session_start rule locks agent down to run_pipeline |
| Q11: Expansion approach | Hard boundary: research agent produces spec, mechanical builder creates tasks |

---

## Stage 1: Single Worktree Per Epic + Bug Fixes

**Goal**: Stop creating N branches/worktrees. One worktree per epic, sequential dispatch. Fix real bugs.

### 1a. Fix orchestrator pipeline — explicit `use_local` and `provider`

**File**: `src/gobby/install/shared/workflows/orchestrator.yaml`

- Modify `create_worktree` step to pass `use_local: true` explicitly
- Pass `provider` from pipeline input so CLI hooks get installed in the worktree

### 1b. Restructure pipeline: one worktree per epic

**File**: `src/gobby/install/shared/workflows/orchestrator.yaml`

- First iteration: `create_worktree` with epic-level branch name, store `_worktree_id`
- Subsequent iterations: Reuse `_worktree_id`
- Merge phase: One merge at epic completion, not per-task
- QA agent gets `worktree_id` so it can see changes

### 1c. Pass worktree context to QA agent

**File**: `src/gobby/install/shared/workflows/orchestrator.yaml`

- Pass `_worktree_id` to QA spawn step

### 1d. Add `use_local` support to clones

**Files**: `src/gobby/clones/git.py`, `src/gobby/mcp_proxy/tools/clones.py`, `src/gobby/agents/isolation.py`

- When `use_local=True`, clone from local repo path instead of remote URL
- Wire through MCP tool and `CloneIsolationHandler`

### Verification

- Run orchestrator on test epic with 3+ subtasks → only 1 worktree/branch
- QA agent can see code changes
- Clone with `use_local=True` has unpushed commits

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
          Use progressive discovery to call run_pipeline.
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

### 3b. `TaskAffectedFileManager`

**New file**: `src/gobby/storage/task_affected_files.py`

Follow `TaskDependencyManager` pattern (`src/gobby/storage/task_dependencies.py`):
- `set_files()`, `get_files()`, `add_file()`, `remove_file()`
- `find_overlapping_tasks()` — given task IDs, returns `{(task_a, task_b): [shared_file_paths]}`
- `get_tasks_for_file()` — reverse lookup

### 3c. Expansion prompt — add `affected_files` and `parallel_group`

**File**: `src/gobby/tasks/prompts/expand-task.md`

Add `affected_files: array[string]` and `parallel_group: string` to subtask schema.

### 3d. Tree builder — wire `affected_files`

**File**: `src/gobby/tasks/tree_builder.py`

In `_create_node()`, after task creation: read `affected_files` from node, call `TaskAffectedFileManager.set_files()`. Store `parallel_group` as label with `parallel:` prefix.

### 3e. MCP tools for affected files

**New file**: `src/gobby/mcp_proxy/tools/tasks/_affected_files.py`

- `set_affected_files(task_id, files, source)`
- `get_affected_files(task_id)`
- `find_file_overlaps(task_ids)`

### Verification

- Unit tests for `TaskAffectedFileManager` (CRUD, overlap detection)
- Expand a test task → `affected_files` stored
- Overlap query returns correct pairs
- Migration applies cleanly

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

**Goal**: `suggest_next_tasks` (plural) returns batches of non-conflicting tasks. Orchestrator dispatches multiple agents simultaneously.

- 5a. `suggest_next_tasks` MCP tool — extract shared scoring from `suggest_next_task`, add file-overlap filtering via `TaskAffectedFileManager.find_overlapping_tasks()`
- 5b. Orchestrator parallel dispatch — replace `suggest_next_task` with `suggest_next_tasks`, dispatch multiple `spawn_developer` calls, format step between batches
- 5c. Post-hoc file annotation update — after task completes, update `task_affected_files` with `annotation_source='observed'` from git diff

### Verification

- 2+ agents dispatched in parallel for non-conflicting tasks
- Conflicting tasks serialized
- Format step runs between batches
- Post-hoc annotations update

---

## Stage 6: Deterministic TDD Enforcement

**Goal**: Replace prompt-based TDD with deterministic rule enforcement. Independent of Stages 2-5.

### 6a. Add Jinja2 rendering to `mcp_call` arguments

**File**: `src/gobby/workflows/rule_engine.py` (~line 420)

Render string values in `effect.arguments` through Jinja2 (consistent with block/inject). ~10 lines.

### 6b. `enforce_tdd` variable definitions

**File**: `src/gobby/install/shared/variables/gobby-default-variables.yaml`

Add `enforce_tdd` (false), `tdd_nudged_files` ([]), `tdd_tests_written` ([]).

### 6c. Rule: `enforce-tdd-block` (before_tool)

**New file**: `src/gobby/install/shared/rules/enforce-tdd-block.yaml`

Blocks Write to new code files until test written. One-shot nudge via `tdd_nudged_files`. Updates `validation_criteria` on task via `mcp_call`.

### 6d. Rule: `enforce-tdd-track-tests` (after_tool)

**New file**: `src/gobby/install/shared/rules/enforce-tdd-track-tests.yaml`

Tracks test files written for observability.

### Verification

- `enforce_tdd=true` → Write to `.py` blocked with TDD message
- Test file write succeeds, tracked
- Retry `.py` succeeds (already nudged)
- Config/init/`.md` files not blocked
- Edit tool not blocked

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
| `src/gobby/install/shared/workflows/orchestrator.yaml` | 1, 4, 5 | Restructure pipeline |
| `src/gobby/clones/git.py` | 1 | Add `use_local` to `create_clone` |
| `src/gobby/mcp_proxy/tools/clones.py` | 1 | Wire `use_local` to MCP tool |
| `src/gobby/agents/isolation.py` | 1 | Auto-detect unpushed in `CloneIsolationHandler` |
| `src/gobby/workflows/definitions.py` | 2 | Remove extends, rule_definitions; add steps to AgentDefinitionBody |
| `src/gobby/workflows/agent_resolver.py` | 2 | Simplify to direct lookup |
| `src/gobby/workflows/loader.py` | 2 | Auto-register agent steps as step workflow |
| `src/gobby/install/shared/agents/*.yaml` | 2 | Migrate all agents to composition + absorb steps |
| `src/gobby/mcp_proxy/tools/agent_definitions.py` | 2 | Add steps to detail/summary, add update_agent_steps |
| `web/src/` (agent editor components) | 2 | Step editor UI for agent definitions |
| `src/gobby/install/shared/rules/pipeline-enforcement/` | 2 | Consolidate to auto-run-pipeline rule |
| `src/gobby/storage/migrations.py` | 3 | Add migration for task_affected_files |
| `src/gobby/storage/task_affected_files.py` | 3 | New manager |
| `src/gobby/tasks/prompts/expand-task.md` | 3 | Add affected_files + parallel_group |
| `src/gobby/tasks/tree_builder.py` | 3 | Wire affected_files |
| `src/gobby/mcp_proxy/tools/tasks/_affected_files.py` | 3, 4 | New MCP tools |
| `src/gobby/install/shared/agents/expander.yaml` | 4 | New expansion agent |
| `src/gobby/install/shared/workflows/expand-task.yaml` | 4 | New expansion pipeline |
| `src/gobby/mcp_proxy/tools/tasks/_expansion.py` | 4 | Add validate_expansion_spec |
| `src/gobby/install/shared/skills/expand/SKILL.md` | 4 | Thin wrapper over pipeline |
| `src/gobby/mcp_proxy/tools/task_readiness.py` | 5 | suggest_next_tasks (plural) |
| `src/gobby/install/shared/variables/gobby-default-variables.yaml` | 6 | Add enforce_tdd vars |
| `src/gobby/install/shared/rules/enforce-tdd-block.yaml` | 6 | New rule template |
| `src/gobby/install/shared/rules/enforce-tdd-track-tests.yaml` | 6 | New rule template |
| `src/gobby/workflows/rule_engine.py` | 6 | Add Jinja2 mcp_call rendering |
| `docs/guides/workflows.md` | 7 | Delete and rewrite — three-part model overview |
| `docs/guides/workflow-rules.md` | 7 | Delete |
| `docs/guides/workflow-actions.md` | 7 | Delete |
| `docs/guides/agent_definitions.md` | 7 | Delete |
| `docs/guides/pipelines.md` | 7 | Delete and rewrite |
| `docs/guides/orchestration.md` | 7 | Delete and rewrite |
| `docs/guides/rules.md` | 7 | New — rules guide |
| `docs/guides/agents.md` | 7 | New — agents guide |
