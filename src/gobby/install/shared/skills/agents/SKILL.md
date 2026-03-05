---
name: agents
description: "Spawn, manage, and message subagents via gobby-agents. Also guides authoring new agent definitions with step workflows. Use when asked to 'build agent', 'create agent definition', 'author agent', 'design agent', or work with agents."
version: "3.0.0"
category: orchestration
triggers: agents, spawn agent, build agent, create agent definition, author agent, design agent
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# Agents Skill

Two-part skill: **Part 1** is a streamlined reference for spawning and managing agents. **Part 2** is an interactive guide for authoring new agent definition YAML files.

---

## Part 1: Using Agents

All agent tools live on **`gobby-agents`** (not `gobby-sessions`). Session IDs accept `#N`, `N`, UUID, or prefix.

### Spawning

```python
call_tool("gobby-agents", "spawn_agent", {
    "prompt": "Implement feature X",
    "agent": "developer",              # Named definition (optional)
    "task_id": "#42",                  # Associate with task (optional)
    "isolation": "worktree",           # none | worktree | clone
    "timeout": 30,                     # Minutes (0 = unlimited)
    "max_turns": 100                   # Agent turns (0 = unlimited)
})
```

**Key parameters:**

| Parameter | Description |
|-----------|-------------|
| `prompt` | **Required.** Task instruction for the agent |
| `agent` | Named definition (e.g., `"developer"`, `"qa-reviewer"`) |
| `task_id` | Gobby task to associate |
| `isolation` | `"none"` (current dir), `"worktree"` (git worktree), `"clone"` (shallow clone) |
| `branch_name` | Custom branch for worktree/clone |
| `base_branch` | Branch to create from |
| `worktree_id` / `clone_id` | Reuse existing isolation |
| `provider` | LLM provider override |
| `model` | Model override |
| `timeout` | Max minutes |
| `max_turns` | Max agent turns |

**Pre-flight check:**
```python
# Check depth limits before spawning
call_tool("gobby-agents", "can_spawn_agent", {"parent_session_id": "#5"})

# Dry-run validation
call_tool("gobby-agents", "evaluate_spawn", {
    "agent": "developer", "isolation": "worktree", "task_id": "#42"
})
```

### Lifecycle

```python
# Kill agent (parent kills child by run_id)
call_tool("gobby-agents", "kill_agent", {"run_id": "ar-abc123"})

# Self-termination (agent kills itself by session_id)
call_tool("gobby-agents", "kill_agent", {"session_id": "#10"})

# Stop (DB status only, doesn't kill process)
call_tool("gobby-agents", "stop_agent", {"run_id": "ar-abc123"})

# Get result
call_tool("gobby-agents", "get_agent_result", {"run_id": "ar-abc123"})
```

### Querying

```python
# List all runs for a session
call_tool("gobby-agents", "list_agents", {"parent_session_id": "#5"})

# List currently running processes
call_tool("gobby-agents", "list_running_agents", {"parent_session_id": "#5"})

# Aggregate stats
call_tool("gobby-agents", "running_agent_stats", {})
```

### Messaging (P2P)

```python
# Send message
call_tool("gobby-agents", "send_message", {
    "from_session": "#10",
    "to_session": "#5",
    "content": "Task complete"
})

# Check inbox
call_tool("gobby-agents", "deliver_pending_messages", {"session_id": "#5"})
```

### Commands (Parent-to-Child)

```python
# Issue command
call_tool("gobby-agents", "send_command", {
    "from_session": "#5",
    "to_session": "#10",
    "command_text": "Run the failing tests and fix them",
    "allowed_tools": ["Bash", "Read", "Edit"]
})

# Accept command (child)
call_tool("gobby-agents", "activate_command", {
    "session_id": "#10", "command_id": "cmd-abc123"
})

# Complete command (child)
call_tool("gobby-agents", "complete_command", {
    "session_id": "#10", "command_id": "cmd-abc123",
    "result": "Fixed 3 test failures"
})
```

### Agent Definitions (CRUD)

```python
# List definitions
call_tool("gobby-workflows", "list_agent_definitions", {"enabled": true})

# Get definition
call_tool("gobby-workflows", "get_agent_definition", {"name": "developer"})

# Create definition
call_tool("gobby-workflows", "create_agent_definition", {
    "name": "my-agent", "definition": { ... }
})

# Toggle enabled/disabled
call_tool("gobby-workflows", "toggle_agent_definition", {
    "name": "my-agent", "enabled": true
})
```

### Dos and Don'ts

- **DO** use `evaluate_spawn` before complex spawns
- **DO** use `can_spawn_agent` to check depth limits
- **DO** call `deliver_pending_messages` before long-running work
- **DO** use `kill_agent` (not bash) to terminate agents
- **DON'T** poll constantly — use event-driven patterns
- **DON'T** send commands to non-descendant sessions (ancestry validated)

---

## Part 2: Building Agent Definitions

Interactive guide for authoring agent definition YAML. Walk through this with the user step by step.

### Step 1: Determine Agent Role

Ask the user:

1. **"What does this agent do?"** — One sentence purpose.
2. **"Is this a worker (spawned by pipelines), a reviewer, or an interactive agent?"**
3. **"Does it need isolated git state?"** — Determines isolation mode.

From answers, determine:
- Agent name (kebab-case)
- Execution mode: `self` (interactive), `terminal` (worker), `autonomous` (background)
- Isolation mode: `none`, `worktree`, `clone`

### Mode Decision Matrix

| Scenario | Mode | Isolation | Why |
|----------|------|-----------|-----|
| Interactive user session | `self` | — | Configures current session, no subprocess |
| Developer working on tasks | `terminal` | `worktree` | Needs isolated branch, visible in tmux |
| Background automation | `autonomous` | `worktree` | No terminal needed |
| Merge agent | `terminal` | `none` | Works in main repo |
| Review-only agent | `terminal` | `none` or `worktree` | May need read-only access to branch |

### Step 2: Design Step Workflow

Ask: **"What phases does this agent go through?"**

Common patterns:

**Simple worker (no phases):**
```yaml
steps:
  - name: work
    allowed_tools: "all"

  - name: terminate
    allowed_mcp_tools: ["gobby-agents:kill_agent"]

exit_condition: "current_step == 'terminate'"
```

**Claim → Implement → Submit (developer pattern):**
```yaml
step_variables:
  task_claimed: false
  review_submitted: false

steps:
  - name: claim
    allowed_tools: [mcp__gobby__call_tool, mcp__gobby__list_mcp_servers, mcp__gobby__list_tools, mcp__gobby__get_tool_schema]
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
    allowed_tools: "all"
    blocked_mcp_tools:
      - "gobby-tasks:close_task"
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
    allowed_mcp_tools: ["gobby-agents:kill_agent"]

exit_condition: "current_step == 'terminate'"
```

**Research → Output (expander pattern):**
```yaml
step_variables:
  spec_saved: false

steps:
  - name: research
    allowed_tools: "all"
    blocked_mcp_tools:
      - "gobby-tasks:execute_expansion"    # Not my job
      - "gobby-tasks:create_task"
      - "gobby-agents:kill_agent"
    on_mcp_success:
      - server: gobby-tasks
        tool: save_expansion_spec
        action: set_variable
        variable: spec_saved
        value: true
    transitions:
      - to: terminate
        when: "vars.spec_saved"

  - name: terminate
    allowed_mcp_tools: ["gobby-agents:kill_agent"]
```

**Review → Decide (QA pattern):**
```yaml
step_variables:
  review_complete: false

steps:
  - name: review
    allowed_tools: "all"
    blocked_mcp_tools:
      - "gobby-tasks:close_task"
      - "gobby-agents:kill_agent"
    on_mcp_success:
      - server: gobby-tasks
        tool: mark_task_review_approved
        action: set_variable
        variable: review_complete
        value: true
      - server: gobby-tasks
        tool: reopen_task
        action: set_variable
        variable: review_complete
        value: true
    transitions:
      - to: terminate
        when: "vars.review_complete"

  - name: terminate
    allowed_mcp_tools: ["gobby-agents:kill_agent"]
```

For each phase, determine:
1. **What tools are allowed?** Use `"all"` for open phases, explicit lists for locked phases.
2. **What tools are blocked?** Block premature exits (close_task, kill_agent) during work phases.
3. **What triggers the transition?** An MCP tool success + variable set + condition.

### Step 3: Configure Selectors

Ask: **"Should this agent use all rules, or a subset?"**

```yaml
workflows:
  # Load all gobby-tagged rules (standard)
  rule_selectors:
    include: ["tag:gobby"]

  # All skills (permissive default)
  # skill_selectors: null

  # All variables (permissive default)
  # variable_selectors: null

  # Override specific variables
  variables:
    enforce_tdd: true
    mode_level: 2
```

Common selector patterns:

| Pattern | Use Case |
|---------|----------|
| `include: ["tag:gobby"]` | Standard — all core rules |
| `include: ["tag:gobby"], exclude: ["name:enforce-tdd-*"]` | Core rules without TDD |
| `include: ["tag:gobby", "tag:pipeline"]` | Core + pipeline-specific rules |
| `include: ["*"]` | Everything (wide open) |

### Step 4: Generate YAML

#### Full Definition Template

```yaml
name: <agent-name>
description: <one-line description>
version: "1.0"
enabled: false                    # Templates are disabled by default
priority: 100

# Identity
role: |
  You are <role description>.
goal: |
  <what the agent should accomplish>
instructions: |
  <specific instructions, workflow guidance>

# Execution
mode: terminal                    # self | terminal | autonomous
isolation: worktree               # none | worktree | clone
provider: inherit                 # inherit | claude | gemini
model: ""                         # Empty = inherit
base_branch: inherit              # inherit | main | specific-branch
timeout: 0                        # Minutes (0 = unlimited)
max_turns: 0                      # Turns (0 = unlimited)

# Step workflow
step_variables:
  <var_name>: <default_value>

steps:
  - name: <step_name>
    description: "<what this step does>"
    status_message: "<shown to agent>"
    allowed_tools: "all"           # or explicit list
    blocked_tools: []
    allowed_mcp_tools: "all"       # or explicit list
    blocked_mcp_tools: []
    on_mcp_success:
      - server: <server>
        tool: <tool>
        action: set_variable
        variable: <var>
        value: <val>
    transitions:
      - to: <next_step>
        when: "vars.<variable>"

  - name: terminate
    description: "Shut down"
    allowed_mcp_tools:
      - "gobby-agents:kill_agent"

exit_condition: "current_step == 'terminate'"

# Selectors
workflows:
  rule_selectors:
    include: ["tag:gobby"]
  variables:
    <override_var>: <value>
```

### Step 5: Validate

Check for common mistakes:

1. **Every agent needs a `terminate` step** — The agent needs a way to exit cleanly.
2. **`kill_agent` must be allowed in terminate step** — Otherwise the agent can never stop.
3. **Discovery tools are always allowed** — Don't block `list_mcp_servers`, `list_tools`, `get_tool_schema`.
4. **`on_mcp_success` must match real tool names** — Server and tool must be exact matches.
5. **Transition conditions reference `vars.`** — Not `variables.` (step workflow uses `vars` prefix).
6. **`step_variables` must declare all transition variables** — Variables used in `when` conditions need defaults.
7. **`exit_condition` uses `current_step`** — Standard pattern: `"current_step == 'terminate'"`.
8. **`mode: self` agents don't need steps** — Self mode configures the current session, not a subprocess.
9. **Agent name is kebab-case** — Convention: `my-agent`, not `myAgent`.

```
Agent Definition Validation:
✓ Terminate step exists with kill_agent allowed
✓ Discovery tools not blocked
✓ on_mcp_success references valid tools
✓ Transition conditions use vars. prefix
✓ All transition variables declared in step_variables
✓ exit_condition is valid
✓ Name follows convention

Ready to install.
```

### Step 6: Install

```python
# Create via MCP
call_tool("gobby-workflows", "create_agent_definition", {
    "name": "<agent-name>",
    "definition": { ... }
})

# Enable it
call_tool("gobby-workflows", "toggle_agent_definition", {
    "name": "<agent-name>",
    "enabled": true
})
```

Or save as YAML and import:
```bash
gobby workflows import my-agent.yaml
```

Tell the user:
```
Agent definition created! To spawn it:

  gobby agents spawn --agent <name> --prompt "..."

Or via MCP:
  call_tool("gobby-agents", "spawn_agent", {
      "agent": "<name>", "prompt": "..."
  })
```

---

## Key Gotchas

1. **Templates are disabled by default** — `enabled: false` is intentional. Enable after import.
2. **`inherit` is the default for provider/model/isolation/mode** — The agent inherits from its parent session.
3. **Step transitions are automatic** — The agent doesn't need to know about transitions. `on_mcp_success` sets variables, `when` conditions trigger transitions.
4. **Depth limit is 5** — Agents spawning agents are capped at 5 levels deep.
5. **`self` mode is special** — Only for the interactive session's own configuration. It doesn't spawn anything.
6. **Block premature exits in work phases** — Block `close_task` and `kill_agent` in implementation steps to prevent agents from short-circuiting.
7. **Both approve and reject can trigger the same transition** — QA agents handle both outcomes through the same variable (review_complete).

## See Also

- [Agents Guide](docs/guides/agents.md) — Full reference
- [Workflows Overview](docs/guides/workflows-overview.md) — How agents fit with rules and pipelines
- [Rules Guide](docs/guides/rules.md) — Rules that constrain agent behavior
- [Orchestrator Guide](docs/guides/orchestrator.md) — Orchestrator pipeline pattern
