---
name: automate
description: "Build automation with Gobby: pipelines, agent definitions, and cron scheduling. Use when asked to 'build pipeline', 'create pipeline', 'build agent', 'create agent definition', 'automate', 'schedule', 'set up cron', or when users describe something they want to happen automatically."
version: "1.0.0"
category: authoring
injectionFormat: full
triggers: automate, build pipeline, create pipeline, author pipeline, write pipeline, design pipeline, build agent, create agent definition, author agent, design agent, schedule, cron, set up cron
metadata:
  category: authoring
---

# Automation Skill

Unified reference for building Gobby automation: **pipelines** (deterministic multi-step workflows), **agent definitions** (spawnable AI workers with step workflows), and **cron scheduling** (timed execution). Use this to translate what a user wants into working automation.

---

## What to Build

| User wants... | Build | Why |
|---------------|-------|-----|
| Sequential steps (build → test → deploy) | Pipeline | Deterministic, resumable, approval gates |
| An AI worker with specific behavior | Agent definition | Step workflow controls tool access, transitions |
| Something to run on a schedule | Cron job | Timed trigger for pipelines, agents, or shell commands |
| Spawn worker → wait → validate result | Pipeline + agent definition | Pipeline orchestrates, agent does the work |
| Periodic maintenance/monitoring | Cron + pipeline | Cron triggers pipeline on schedule |
| AI worker that runs periodically | Cron + agent spawn | Cron spawns agent on schedule |

**Composition rule:** Pipelines orchestrate. Agents do work. Cron triggers things on a schedule. Most real automation combines two or three.

---

## Part 1: Pipelines

Pipelines are deterministic, sequential workflows with data flow between steps.

### Pipeline YAML Structure

```yaml
name: my-pipeline              # Kebab-case, unique
type: pipeline
version: "1.0"
description: What it does

inputs:
  param_name: default_value    # Overridden at runtime

outputs:
  result: "${{ steps.step_two.output }}"

steps:
  - id: step_one
    exec: "npm run build"      # Exactly ONE execution type per step

  - id: step_two
    mcp:
      server: gobby-tasks
      tool: list_tasks
      arguments:
        status: "open"

expose_as_tool: false           # true = callable as MCP tool
resume_on_restart: false        # true = auto-resume after daemon restart
```

### Step Types

Each step has **exactly one** execution type:

| Type | Field | Use |
|------|-------|-----|
| Shell command | `exec` | Build, test, deploy, file ops |
| LLM prompt | `prompt` | Analysis, reasoning, generation |
| MCP tool call | `mcp` | Task ops, agent spawning, memory |
| Sub-pipeline | `invoke_pipeline` | Reusable sub-workflows |
| Wait for async | `wait` | Block until agent/process completes |
| Activate workflow | `activate_workflow` | Set up step workflow on session |

### Step Fields

```yaml
- id: unique_step_id           # Required, unique within pipeline
  exec: "command"              # OR prompt/mcp/invoke_pipeline/wait/activate_workflow
  condition: "${{ expr }}"     # Skip if false (fails-open: errors = step runs)
  approval:                    # Gate requiring human approval
    required: true
    message: "Approve?"
    timeout_seconds: 300
  tools: [Read, Grep]          # Tool restrictions for prompt steps only
```

### Template Variables

Available in `${{ }}` expressions within steps:

| Variable | Description |
|----------|-------------|
| `inputs.<param>` | Pipeline input parameter |
| `steps.<id>.output` | Full output from completed step |
| `steps.<id>.output.<field>` | Nested field (dot notation) |
| `env.<VAR>` | Environment variable (sensitive ones filtered) |
| `session_id` | Pipeline's session ID |
| `parent_session_id` | Caller's session ID |
| `project_id` | Project context |
| `project_path` | Filesystem path to project |
| `current_branch` | Current git branch |

**Type coercion** after rendering: `"true"/"false"` → bool, `"null"` → None, `"600"` → int.

### Common Pipeline Patterns

**Shell build-test-deploy:**
```yaml
steps:
  - id: build
    exec: npm run build
  - id: test
    exec: npm test
  - id: deploy
    exec: deploy-to-prod
    approval:
      required: true
      message: "Approve production deployment?"
```

**Spawn agent and wait for result:**
```yaml
steps:
  - id: spawn
    mcp:
      server: gobby-agents
      tool: spawn_agent
      arguments:
        agent: "developer"
        task_id: "${{ inputs.task_id }}"
  - id: wait
    wait:
      completion_id: "${{ steps.spawn.output.run_id }}"
      timeout: 600
```

**Conditional step:**
```yaml
- id: deploy_prod
  condition: "${{ inputs.environment == 'production' }}"
  exec: deploy --env production
```

**MCP tool call with data flow:**
```yaml
- id: scan
  mcp:
    server: gobby-tasks
    tool: list_tasks
    arguments:
      parent_task_id: "${{ inputs.epic_id }}"
      status: "open"
- id: analyze
  prompt: |
    Analyze these tasks: ${{ steps.scan.output }}
    Prioritize by dependency order.
```

**Sub-pipeline with arguments:**
```yaml
- id: merge
  invoke_pipeline:
    name: merge-clone
    arguments:
      clone_id: "${{ steps.spawn.output.clone_id }}"
```

**Recursive loop with iteration guard:**
```yaml
name: orchestrator
inputs:
  max_iterations: 200
  _current_iteration: 0
steps:
  - id: guard
    condition: "${{ inputs._current_iteration >= inputs.max_iterations }}"
    exec: "echo 'Max iterations' && exit 1"
  - id: work
    mcp: { server: gobby-tasks, tool: list_tasks, arguments: { status: "open" } }
  - id: recurse
    condition: "${{ steps.work.output.tasks | length > 0 }}"
    invoke_pipeline:
      name: orchestrator
      arguments:
        _current_iteration: "${{ inputs._current_iteration + 1 }}"
```

### Pipeline Gotchas

1. **`exec` has no shell features** — No pipes, redirects, or globs. Use `bash -c '...'` if needed.
2. **Nested pipeline outputs don't propagate** — Downstream steps only see `execution_id` and `status`.
3. **Conditions fail-open** — Expression errors mean the step **runs**, not skips.
4. **Sensitive env vars filtered** — Suffixes `_SECRET`, `_KEY`, `_TOKEN`, `_PASSWORD`, `_AUTH`, `_API_KEY` stripped.
5. **`wait` timeout defaults to 600s** — Set higher for long-running agents.
6. **Nesting depth limit: 10** — Prevents runaway recursion. Self-recursion bounded by depth.
7. **Resume replays from start** — Completed steps auto-skip, but all steps should be idempotent.

---

## Part 2: Agent Definitions

Agent definitions configure spawnable AI workers with identity, execution mode, and step workflows that control tool access and phase transitions.

### Agent Definition YAML Structure

```yaml
name: my-agent                  # Kebab-case, unique
description: What this agent does
version: "1.0"
enabled: false                  # Templates disabled by default — enable after install
priority: 100

# Identity (composed into agent's preamble at spawn)
role: |
  You are a <role description>.
goal: |
  <what the agent should accomplish>
personality: |
  <tone and behavior>
instructions: |
  <specific workflow guidance>

# Execution
mode: interactive                # interactive | autonomous
isolation: worktree             # none | worktree | clone
provider: inherit               # inherit | claude | gemini | codex
model: ""                       # Empty = inherit from parent
base_branch: inherit            # inherit | main | specific-branch
timeout: 0                      # Minutes (0 = unlimited)
max_turns: 0                    # Turns (0 = unlimited)

# Step Workflow (controls tool access and phase transitions)
step_variables:
  task_claimed: false

steps:
  - name: claim
    description: "Claim the assigned task"
    status_message: "Claim your task before starting work."
    allowed_mcp_tools: ["gobby-tasks:claim_task", "gobby-tasks:get_task"]
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
    allowed_tools: "all"
    blocked_mcp_tools: ["gobby-tasks:close_task", "gobby-agents:kill_agent"]

  - name: terminate
    allowed_mcp_tools: ["gobby-agents:kill_agent"]

exit_condition: "current_step == 'terminate'"

# Rule/skill selection
workflows:
  rule_selectors:
    include: ["tag:gobby"]
  variables:
    enforce_tdd: true
```

### Mode Decision Matrix

| Scenario | Mode | Isolation | Why |
|----------|------|-----------|-----|
| Developer working on tasks | `interactive` | `worktree` | Isolated branch, visible in tmux |
| Background automation | `autonomous` | `worktree` | No terminal needed |
| Merge agent | `interactive` | `none` | Works in main repo |
| Review-only agent | `autonomous` | `none` | Read-only, no branch needed |

### Step Workflow Mechanics

Steps control **what an agent can do at each phase** via tool restrictions and automatic transitions.

**How transitions work:**
1. Agent calls an MCP tool (e.g., `claim_task`)
2. Tool succeeds → `on_mcp_success` handler fires → sets variable (`task_claimed = true`)
3. Rule engine evaluates `when` condition (`vars.task_claimed`)
4. Condition true → agent moves to next step automatically
5. Agent doesn't need to know about transitions — they're invisible

**Step fields:**

```yaml
- name: step_name               # Unique within agent
  description: "Human label"
  status_message: |             # Shown to agent as context
    Instructions for this phase.
  allowed_tools: "all"          # "all" or explicit list
  blocked_tools: []             # Block specific Claude tools
  allowed_mcp_tools: "all"      # "all" or ["server:tool", ...]
  blocked_mcp_tools: []         # Block specific MCP tools
  on_enter: []                  # Actions on step entry
  on_exit: []                   # Actions on step exit
  on_mcp_success:               # Handlers for specific tool successes
    - server: gobby-tasks
      tool: claim_task
      action: set_variable
      variable: task_claimed
      value: true
  on_mcp_error: []              # Handlers for tool errors
  transitions:
    - to: next_step
      when: "vars.task_claimed" # Uses vars. prefix, NOT variables.
      on_transition: []
```

### Common Agent Step Patterns

**Claim → Implement → Submit (developer):**
```yaml
step_variables:
  task_claimed: false
  review_submitted: false
steps:
  - name: claim
    allowed_mcp_tools: ["gobby-tasks:claim_task", "gobby-tasks:get_task"]
    on_mcp_success:
      - { server: gobby-tasks, tool: claim_task, action: set_variable, variable: task_claimed, value: true }
    transitions:
      - { to: implement, when: "vars.task_claimed" }
  - name: implement
    allowed_tools: "all"
    blocked_mcp_tools: ["gobby-tasks:close_task", "gobby-agents:kill_agent"]
    on_mcp_success:
      - { server: gobby-tasks, tool: mark_task_needs_review, action: set_variable, variable: review_submitted, value: true }
    transitions:
      - { to: terminate, when: "vars.review_submitted" }
  - name: terminate
    allowed_mcp_tools: ["gobby-agents:kill_agent"]
exit_condition: "current_step == 'terminate'"
```

**Review → Decide (QA):**
```yaml
step_variables:
  review_complete: false
steps:
  - name: review
    allowed_tools: "all"
    blocked_mcp_tools: ["gobby-tasks:close_task", "gobby-agents:kill_agent"]
    on_mcp_success:
      - { server: gobby-tasks, tool: mark_task_review_approved, action: set_variable, variable: review_complete, value: true }
      - { server: gobby-tasks, tool: reopen_task, action: set_variable, variable: review_complete, value: true }
    transitions:
      - { to: terminate, when: "vars.review_complete" }
  - name: terminate
    allowed_mcp_tools: ["gobby-agents:kill_agent"]
exit_condition: "current_step == 'terminate'"
```

**Research → Output (expander):**
```yaml
step_variables:
  run_started: false
steps:
  - name: research
    allowed_tools: "all"
    blocked_mcp_tools: ["gobby-tasks:create_task", "gobby-agents:kill_agent"]
    on_mcp_success:
      - { server: gobby-tasks-expansion, tool: start_expansion_run, action: set_variable, variable: run_started, value: true }
    transitions:
      - { to: terminate, when: "vars.run_started" }
  - name: terminate
    allowed_mcp_tools: ["gobby-agents:kill_agent"]
exit_condition: "current_step == 'terminate'"
```

**Simple worker (no phases):**
```yaml
steps:
  - name: work
    allowed_tools: "all"
  - name: terminate
    allowed_mcp_tools: ["gobby-agents:kill_agent"]
exit_condition: "current_step == 'terminate'"
```

### Spawning Agents

```python
# Spawn with named definition
call_tool("gobby-agents", "spawn_agent", {
    "prompt": "Implement feature X",
    "agent": "developer",
    "task_id": "#42",
    "isolation": "worktree",
    "timeout": 30,
    "max_turns": 100
})

# Pre-flight checks
call_tool("gobby-agents", "can_spawn_agent", {"parent_session_id": "#5"})
call_tool("gobby-agents", "evaluate_spawn", {
    "agent": "developer", "isolation": "worktree", "task_id": "#42"
})

# Lifecycle
call_tool("gobby-agents", "kill_agent", {"run_id": "ar-abc123"})
call_tool("gobby-agents", "get_agent_result", {"run_id": "ar-abc123"})
call_tool("gobby-agents", "list_running_agents", {"parent_session_id": "#5"})

# Messaging (P2P)
call_tool("gobby-agents", "send_message", {
    "from_session": "#10", "to_session": "#5", "content": "Done"
})
call_tool("gobby-agents", "deliver_pending_messages", {"target_session_id": "#5"})
```

### Agent Definition Gotchas

1. **Templates disabled by default** — `enabled: false` is intentional. Enable after install.
2. **`inherit` is the default** — Provider, model, base_branch all inherit from parent.
3. **Transitions are invisible to the agent** — The agent just uses tools; the rule engine handles phase transitions automatically.
4. **Depth limit: 5** — Agents spawning agents capped at 5 levels.
5. **Both modes spawn** — `interactive` opens a visible tmux session, `autonomous` runs headless with auto-approve.
6. **Block premature exits** — Block `close_task` and `kill_agent` in work phases.
7. **Both outcomes can share a transition** — QA agents: approve and reject both set `review_complete`.
8. **Discovery tools always allowed** — Never block `list_mcp_servers`, `list_tools`, `get_tool_schema`.

---

## Part 3: Cron Scheduling

Cron jobs trigger automation on a schedule — pipelines, agent spawns, or shell commands.

### Schedule Types

| Type | Field | Example |
|------|-------|---------|
| Cron expression | `cron_expr` | `"0 7 * * *"` (daily at 7am) |
| Fixed interval | `interval_seconds` | `300` (every 5 minutes) |
| One-shot | `run_at` | `"2026-03-15T10:00:00"` (ISO 8601) |

**Cron expression format** (5-field, via croniter):
```
┌───────────── minute (0-59)
│ ┌───────────── hour (0-23)
│ │ ┌───────────── day of month (1-31)
│ │ │ ┌───────────── month (1-12)
│ │ │ │ ┌───────────── day of week (0-6, MON-SUN)
│ │ │ │ │
* * * * *
```

Examples: `*/15 * * * *` (every 15min), `0 0 * * 0` (weekly Sunday midnight), `30 2 * * MON-FRI` (weekdays 2:30am).

### Action Types

| Type | Config | Use |
|------|--------|-----|
| `pipeline` | `{"pipeline_name": "...", "inputs": {...}}` | Run a pipeline |
| `agent_spawn` | `{"prompt": "...", "provider": "claude", "agent_definition": "...", "timeout_seconds": 300}` | Spawn a headless agent |
| `shell` | `{"command": "...", "args": [...], "cwd": "...", "timeout_seconds": 60}` | Run a shell command |
| `handler` | `{"handler": "handler_name"}` | Call a registered async handler |

### Creating Cron Jobs via MCP

```python
# Schedule a pipeline to run daily
call_tool("gobby-cron", "create_cron_job", {
    "name": "nightly-scan",
    "schedule_type": "cron",
    "cron_expr": "0 2 * * *",
    "timezone": "America/New_York",
    "action_type": "pipeline",
    "action_config": {
        "pipeline_name": "scan-and-report",
        "inputs": {"scope": "all"}
    },
    "description": "Nightly codebase scan"
})

# Schedule an agent to run every 30 minutes
call_tool("gobby-cron", "create_cron_job", {
    "name": "monitor-tasks",
    "schedule_type": "interval",
    "interval_seconds": 1800,
    "action_type": "agent_spawn",
    "action_config": {
        "prompt": "Check for stalled tasks and alert",
        "provider": "claude",
        "agent_definition": "monitor",
        "timeout_seconds": 300
    }
})

# One-shot scheduled task
call_tool("gobby-cron", "create_cron_job", {
    "name": "release-cut",
    "schedule_type": "once",
    "run_at": "2026-03-15T10:00:00",
    "timezone": "UTC",
    "action_type": "pipeline",
    "action_config": {
        "pipeline_name": "cut-release",
        "inputs": {"version": "2.0.0"}
    }
})
```

### Managing Cron Jobs

```python
# List jobs
call_tool("gobby-cron", "list_cron_jobs", {"enabled": true})

# Toggle on/off
call_tool("gobby-cron", "toggle_cron_job", {"job_id": "..."})

# Update schedule
call_tool("gobby-cron", "update_cron_job", {
    "job_id": "...",
    "cron_expr": "0 */4 * * *"   # Change to every 4 hours
})

# Trigger immediately (for testing)
call_tool("gobby-cron", "run_cron_job", {"job_id": "..."})

# Check execution history
call_tool("gobby-cron", "list_cron_runs", {"job_id": "...", "limit": 10})

# Delete
call_tool("gobby-cron", "delete_cron_job", {"job_id": "..."})
```

### Cron Behavior

- **Exponential backoff** on consecutive failures: 30s → 60s → 5m → 15m → 1h
- **Max concurrent jobs**: 5 (configurable)
- **Run history retention**: 30 days (auto-cleanup)
- **Scheduler check interval**: 30 seconds
- **Timezone-aware**: All jobs support timezone (default UTC)
- Consecutive failures reset on success

---

## Part 4: Composition Patterns

Real automation usually combines pipelines + agents + cron. These patterns show how.

### Pattern 1: Scheduled Pipeline That Spawns Workers

**Use case:** Every night, scan for open tasks and spawn developers to work on them.

**Components:**
1. Pipeline: `nightly-dispatch` — scans tasks, spawns agents, waits
2. Agent definition: `developer` — claims and implements tasks
3. Cron job: triggers pipeline nightly

```yaml
# Pipeline: nightly-dispatch
name: nightly-dispatch
type: pipeline
version: "1.0"
description: Scan open tasks and spawn developers

steps:
  - id: scan
    mcp:
      server: gobby-tasks
      tool: list_tasks
      arguments:
        status: "open"
        category: "code"

  - id: dispatch
    condition: "${{ steps.scan.output.tasks | length > 0 }}"
    prompt: |
      Given these open tasks: ${{ steps.scan.output }}
      For each task, spawn a developer agent.
      Use spawn_agent with agent="developer" and the task_id.
    tools: [mcp__gobby__call_tool]

  - id: report
    mcp:
      server: gobby-memory
      tool: create_memory
      arguments:
        content: "Dispatched developers for ${{ steps.scan.output.tasks | length }} tasks"
        tags: ["automation", "dispatch"]
```

```python
# Cron: trigger nightly
call_tool("gobby-cron", "create_cron_job", {
    "name": "nightly-dispatch",
    "schedule_type": "cron",
    "cron_expr": "0 2 * * *",
    "action_type": "pipeline",
    "action_config": {"pipeline_name": "nightly-dispatch"}
})
```

### Pattern 2: Orchestrator Loop

**Use case:** Continuously process tasks until an epic is complete.

**Components:**
1. Pipeline: `orchestrator` — recursive loop that spawns workers and waits
2. Agent definitions: `developer`, `qa-reviewer`
3. Cron job: periodic trigger (or run once manually)

This is the orchestrator pattern — the pipeline calls itself recursively with an iteration counter, spawning workers for open tasks and waiting for completion each pass.

### Pattern 3: Periodic Health Check

**Use case:** Every hour, run tests and alert on failures.

```python
# Simple: cron + shell
call_tool("gobby-cron", "create_cron_job", {
    "name": "hourly-tests",
    "schedule_type": "interval",
    "interval_seconds": 3600,
    "action_type": "shell",
    "action_config": {
        "command": "bash",
        "args": ["-c", "cd /path/to/project && uv run pytest tests/smoke/ -x --tb=short"],
        "timeout_seconds": 300
    }
})
```

### Pattern 4: Agent-Driven Scheduled Review

**Use case:** Weekly code review of recent changes.

```python
# Cron spawns agent directly (no pipeline needed for simple cases)
call_tool("gobby-cron", "create_cron_job", {
    "name": "weekly-review",
    "schedule_type": "cron",
    "cron_expr": "0 9 * * MON",
    "timezone": "America/New_York",
    "action_type": "agent_spawn",
    "action_config": {
        "prompt": "Review all commits from the past week. Create tasks for any issues found.",
        "provider": "claude",
        "agent_definition": "qa-reviewer",
        "timeout_seconds": 600
    }
})
```

---

## Part 5: Installation & Testing

> **CRITICAL: Always create template files.** DB-only definitions created via MCP are
> ephemeral — they get purged on daemon restart when `sync_bundled_content_to_db` runs.
> The correct workflow is: write a YAML template file, commit it, then the sync process
> installs it to the DB automatically. MCP creation tools (`create_pipeline`,
> `create_agent_definition`) are for **testing and iteration only**, not production use.

### Installing Pipelines

**Step 1: Write the YAML template file:**
```bash
# Save to the shared templates directory
src/gobby/install/shared/workflows/pipelines/my-pipeline.yaml
```

**Step 2: Sync to DB** (happens automatically on daemon restart, or manually):
```python
call_tool("gobby-workflows", "reload_cache", {})
```

**Step 3: Enable it** (templates are disabled by default):
```python
call_tool("gobby-workflows", "update_pipeline", {
    "name": "my-pipeline",
    "enabled": true
})
```

For quick iteration/testing only (NOT for production — will be lost on restart):
```python
call_tool("gobby-workflows", "create_pipeline", {
    "yaml_content": "..."  # Pipeline YAML string
})
```

### Installing Agent Definitions

**Step 1: Write the YAML template file:**
```bash
# Save to the shared templates directory
src/gobby/install/shared/workflows/agents/my-agent.yaml
```

**Step 2: Sync to DB** (automatic on restart, or manual):
```python
call_tool("gobby-workflows", "reload_cache", {})
```

**Step 3: Enable it:**
```python
call_tool("gobby-workflows", "toggle_agent_definition", {
    "name": "my-agent",
    "enabled": true
})
```

### Testing

**Test a pipeline:**
```python
# Run with test inputs
call_tool("gobby-workflows", "run_pipeline", {
    "name": "my-pipeline",
    "inputs": {"param": "test-value"}
})

# Check status
call_tool("gobby-workflows", "get_pipeline_status", {"execution_id": "pe-..."})

# Search for failures
call_tool("gobby-workflows", "search_pipeline_executions", {
    "query": "my-pipeline", "status": "failed"
})
```

**Test an agent definition:**
```python
# Dry-run validation
call_tool("gobby-agents", "evaluate_spawn", {
    "agent": "my-agent", "isolation": "worktree"
})

# Spawn with short timeout for testing
call_tool("gobby-agents", "spawn_agent", {
    "agent": "my-agent",
    "prompt": "Test: claim task #99 and report what you find",
    "timeout": 5
})
```

**Test a cron job:**
```python
# Trigger immediately (doesn't wait for schedule)
call_tool("gobby-cron", "run_cron_job", {"job_id": "..."})

# Check run result
call_tool("gobby-cron", "list_cron_runs", {"job_id": "...", "limit": 1})
```

---

## Validation Checklists

### Pipeline Validation

1. Each step has exactly one execution type
2. All `${{ }}` references resolve to prior steps (no forward references)
3. Step IDs are unique
4. Conditions use `${{ }}` syntax (not bare `{{ }}`)
5. MCP steps have `server` and `tool`
6. `wait` steps have `completion_id`
7. Approval gates on steps with side effects (deploy, merge, delete)
8. Pipeline name is kebab-case
9. `invoke_pipeline` with arguments uses dict form: `{name: "...", arguments: {...}}`

### Agent Definition Validation

1. `terminate` step exists with `kill_agent` in `allowed_mcp_tools`
2. Discovery tools never blocked (`list_mcp_servers`, `list_tools`, `get_tool_schema`)
3. `on_mcp_success` references real server:tool pairs
4. Transition conditions use `vars.` prefix (not `variables.`)
5. All transition variables declared in `step_variables` with defaults
6. `exit_condition` is valid (standard: `"current_step == 'terminate'"`)
7. Agent name is kebab-case
8. Both modes spawn — `interactive` opens a tmux session, `autonomous` runs headless

### Cron Job Validation

1. Schedule type matches provided fields (`cron` needs `cron_expr`, `interval` needs `interval_seconds`, `once` needs `run_at`)
2. Cron expression is valid 5-field format
3. Action type matches config shape
4. Pipeline name exists (for `pipeline` action type)
5. Agent definition exists and is enabled (for `agent_spawn` with `agent_definition`)
6. Timeout is reasonable for the action
7. Job name is descriptive and unique

---

## Selector Reference (Agent Definitions)

Control which rules, skills, and variables agents load:

```yaml
workflows:
  rule_selectors:
    include: ["tag:gobby"]                    # Standard core rules
    exclude: ["name:enforce-tdd-*"]           # Opt out of specific rules
  skill_selectors: null                       # null = permissive (all skills)
  variable_selectors: null                    # null = permissive (all variables)
  variables:                                  # Pre-seed session variables
    enforce_tdd: true
    mode_level: 2
```

| Pattern | Use Case |
|---------|----------|
| `include: ["tag:gobby"]` | Standard — all core rules |
| `include: ["tag:gobby", "tag:pipeline"]` | Core + domain-specific |
| `exclude: ["tag:sync"]` | Drop specific tags |
| `include: ["*"]` | Everything (wide open) |
