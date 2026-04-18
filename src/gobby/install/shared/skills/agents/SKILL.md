---
name: agents
description: Spawn, manage, and message subagents via gobby-agents. Also guides authoring new agent definitions with step workflows. Use when asked to 'build agent', 'create agent definition', 'author agent', 'design agent', or work with agents.
category: orchestration
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# Agents — Decisions, Patterns, and Authoring

Two parts: **Part 1** covers when and how to use agents effectively. **Part 2** is an interactive guide for authoring agent definitions.

All agent tools live on **gobby-agents**. Definition CRUD is on **gobby-workflows**. Use progressive discovery for schemas.

---

## Part 1: Using Agents

### When to Spawn vs Other Approaches

| Situation | Approach | Why |
|-----------|----------|-----|
| Parallel independent tasks | **Spawn agents** | Each gets isolated context and git state |
| Sequential subtasks | **Do them yourself** | Spawning overhead isn't worth it for serial work |
| Need a different perspective (review, QA) | **Spawn agent** | Separate context prevents confirmation bias |
| Quick lookup or one-off | **Don't spawn** | Agent startup cost exceeds task cost |
| Risky/experimental work | **Spawn with isolation** | Worktree/clone protects main repo |

### Isolation Strategies

| Mode | What it does | When to use |
|------|-------------|-------------|
| `none` | Same directory as parent | Merge agents, reviewers, anything that reads but doesn't write |
| `worktree` | Git worktree (shared objects, separate branch) | Default for development work. Lightweight, fast cleanup |
| `clone` | Full shallow clone (separate repo copy) | When you need complete isolation from main repo state |

**Default to worktree** unless you have a reason not to. Worktrees work with all supported CLIs.

### Communication Patterns

**Messages (P2P)**: Any session can message any other session. Fire-and-forget. Good for status updates, completion notifications.

**Commands (Parent→Child)**: Structured task delegation with tool restrictions. The parent issues a command, the child activates it (which restricts their tools), does the work, and completes it. Good for controlled delegation.

**Key difference**: Messages are advisory. Commands are authoritative — they change the child's tool access.

### Pre-Flight Checks

Always check before spawning:
- **Depth limits**: Agent chains are capped at 5 levels. Check with `can_spawn_agent`.
- **Dry run**: Use `evaluate_spawn` for complex spawns to validate config before committing.

### Lifecycle

- **Kill vs Stop**: `kill_agent` terminates the process. `stop_agent` only updates DB status. Use kill for actual termination.
- **Self-termination**: Agents kill themselves by session_id. Parents kill children by run_id.
- **Results**: Use `get_agent_result` after the agent exits to retrieve its output.

### Dos and Don'ts

- **DO** use `evaluate_spawn` before complex spawns
- **DO** use `can_spawn_agent` to check depth limits
- **DO** check `deliver_pending_messages` before long-running work
- **DO** use `kill_agent` (not bash) to terminate agents
- **DON'T** poll constantly — use event-driven patterns
- **DON'T** send commands to non-descendant sessions (ancestry is validated)
- **DON'T** spawn for work you could do faster yourself

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
- Execution mode: `interactive` (visible tmux session), `autonomous` (background, auto-approve)
- Isolation mode: `none`, `worktree`, `clone`

### Mode Decision Matrix

| Scenario | Mode | Isolation | Why |
|----------|------|-----------|-----|
| Developer working on tasks | `interactive` | `worktree` | Isolated branch, visible in tmux |
| Background automation | `autonomous` | `worktree` | Headless, auto-approve |
| Merge agent | `interactive` | `none` | Works in main repo, visible in tmux |
| Review-only agent | `autonomous` | `none` or `worktree` | Headless, auto-approve for read-only review |

### Step 2: Design Step Workflow

Ask: **"What phases does this agent go through?"**

Every agent needs at minimum a work step and a terminate step. Common patterns:

**Simple worker:**
```yaml
steps:
  - name: work
    allowed_tools: "all"
  - name: terminate
    allowed_mcp_tools: ["gobby-agents:kill_agent"]
exit_condition: "current_step == 'terminate'"
```

**Claim → Implement → Submit (developer pattern):**
- `claim` step: Only allow task claiming tools. Transition on `task_claimed`.
- `implement` step: All tools, but block `close_task` and `kill_agent`. Transition on `review_submitted`.
- `terminate` step: Only `kill_agent`.

**Research → Persist Result:**
- `research` step: All tools, block premature exits. Transition on `result_saved`.
- `terminate` step.

**Review → Decide (QA pattern):**
- `review` step: All tools, block `close_task`. Transition on `review_complete` (triggered by either approve or reject).
- `terminate` step.

For each phase, determine:
1. **What tools are allowed?** Use `"all"` for open phases, explicit lists for locked phases.
2. **What tools are blocked?** Block premature exits (`close_task`, `kill_agent`) during work phases.
3. **What triggers the transition?** `on_mcp_success` sets a variable, `when` condition triggers the move.

### Step 3: Configure Selectors

Ask: **"Should this agent use all rules, or a subset?"**

| Pattern | Use Case |
|---------|----------|
| `include: ["tag:gobby"]` | Standard — all core rules |
| `include: ["tag:gobby"], exclude: ["name:enforce-tdd-*"]` | Core rules without TDD |
| `include: ["tag:gobby", "tag:pipeline"]` | Core + pipeline-specific rules |
| `include: ["*"]` | Everything (wide open) |

### Step 4: Generate YAML

Key fields in the definition:

```yaml
name: <agent-name>           # kebab-case
description: <one line>
version: "1.0"
enabled: false               # Templates disabled by default

# Identity
role: |
  You are <role description>.
goal: |
  <what the agent should accomplish>
instructions: |
  <specific guidance>

# Execution
mode: interactive             # interactive | autonomous
isolation: worktree           # none | worktree | clone
provider: inherit
timeout: 0                    # Minutes (0 = unlimited)
max_turns: 0                  # Turns (0 = unlimited)

# Step workflow
step_variables:
  <var>: <default>

steps:
  - name: <step>
    allowed_tools: "all"
    blocked_mcp_tools: []
    on_mcp_success:
      - server: <server>
        tool: <tool>
        action: set_variable
        variable: <var>
        value: <val>
    transitions:
      - to: <next>
        when: "vars.<variable>"

  - name: terminate
    allowed_mcp_tools: ["gobby-agents:kill_agent"]

exit_condition: "current_step == 'terminate'"

workflows:
  rule_selectors:
    include: ["tag:gobby"]
```

### Step 5: Validate

Check for common mistakes:

1. **Every agent needs a `terminate` step** with `kill_agent` allowed
2. **Discovery tools are always allowed** — don't block `list_mcp_servers`, `list_tools`, `get_tool_schema`
3. **`on_mcp_success` must match real tool names** — server and tool must be exact
4. **Transition conditions use `vars.` prefix** — not `variables.`
5. **All transition variables declared in `step_variables`** with defaults
6. **Block premature exits in work phases** — `close_task` and `kill_agent` during implementation
7. **Both modes spawn via tmux** — `interactive` opens a visible tmux session, `autonomous` runs headless with auto-approve

### Step 6: Install

Create via MCP on gobby-workflows (`create_agent_definition`), then enable with `toggle_agent_definition`. Or save as YAML and import via CLI.

## Key Gotchas

1. **Templates are disabled by default** — `enabled: false` is intentional. Enable after import.
2. **`inherit` is the default** for provider/model/isolation/mode — agent inherits from parent.
3. **Transitions are automatic** — `on_mcp_success` sets variables, `when` conditions fire transitions. The agent doesn't manage this.
4. **Depth limit is 5** — agents spawning agents, capped.
5. **Both approve and reject can trigger the same transition** — QA agents handle both outcomes through one variable.
