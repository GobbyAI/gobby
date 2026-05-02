# Agents

Agents are reusable worker definitions that Gobby can apply to the current
session or use to spawn child sessions. The model is **CLI agnostic**:
the same agent definition system is used across Claude, Codex, and Gemini
integrations.

Current Gobby splits agents into two concerns:

- **Definition management** on `gobby-workflows`
- **Runtime execution** on `gobby-agents`

For the broader architecture, see [Workflows Overview](./workflows-overview.md).

## Two Ways Agents Are Used

### Apply A Persona To The Current Session

Use `gobby-agents:apply_persona` when you want the current session to adopt an
agent definition without creating a child process.

That applies:

- prompt fields such as `role` and `instructions`
- matching rules and skills
- workflow variables
- inline step workflow state, if the agent defines steps

This is how baseline interactive personas such as `default` are applied.

### Spawn A Child Agent

Use `gobby-agents:spawn_agent` or `dispatch_batch` when you want a separate
worker session, optionally in a worktree or clone.

That creates:

- a child session
- optional isolation (`none`, `worktree`, or `clone`)
- an agent run record
- a completion event that parents can wait on

## Agent Definition Shape

Agent definitions live in `workflow_definitions` with `workflow_type='agent'`
and are managed through `gobby-workflows`.

### Core Fields

| Field | Purpose |
| --- | --- |
| `name` | Unique definition name |
| `description` | Human-readable summary |
| `role` | `## Role` block in the generated preamble |
| `goal` | `## Goal` block in the generated preamble |
| `personality` | `## Personality` block in the generated preamble |
| `instructions` | `## Instructions` block in the generated preamble |
| `provider` | Preferred provider, or `inherit` |
| `model` | Optional model override |
| `fallback_agent` | Optional fallback definition |
| `api_base` / `api_token` | Optional custom endpoint configuration |
| `isolation` | `none`, `worktree`, `clone`, or `inherit` |
| `base_branch` | Branch to isolate from, or `inherit` |
| `timeout` | Max run time in seconds/minutes as configured by runtime |
| `max_turns` | Turn cap (`0` means unlimited) |
| `blocked_tools` / `blocked_mcp_tools` | Agent-level tool restrictions |
| `workflows` | Rule selectors, skill selectors, variable selectors, and seeded variables |
| `steps` | Optional inline step workflow |
| `step_variables` | Initial variables for the inline step workflow |
| `exit_condition` | Optional terminal condition for the workflow |
| `enabled` | Whether the definition is active |
| `sources` | Optional session-source filter |

### Important Current Detail

Older drafts and examples sometimes refer to a `mode` field such as `self`,
`interactive`, or `autonomous`. That is **not part of the current
`AgentDefinitionBody` schema**. New agent definitions should not rely on it.

The practical split now is:

- `apply_persona` for current-session application
- `spawn_agent` / `dispatch_batch` for child-session execution

### Validation Tightening In `v0.4.0`

The agent definition schema now validates several execution fields more
strictly. The following fields must already be the correct YAML types and are no
longer coerced from loosely-typed values:

- `model`
- `reasoning_effort`
- `reasoning_required`
- `fallback_agent`
- `api_base`
- `api_token`

Practical impact:

- values that used to be accepted after implicit coercion now fail validation
- string fields must be real YAML strings
- `reasoning_required` must be a real YAML boolean (`true` / `false`)

Before:

```yaml
model: 1234
reasoning_effort: 2
reasoning_required: "false"
fallback_agent: 0
api_base: 12345
api_token: false
```

After:

```yaml
model: "1234"
reasoning_effort: "medium"
reasoning_required: false
fallback_agent: "backup-agent"
api_base: "http://localhost:1234/v1"
api_token: "${LM_STUDIO_API_KEY}"
```

If you have older YAML that relied on coercion, quote string-like values and
convert boolean-like strings to explicit YAML booleans. This validation is
enforced by the current `AgentDefinitionBody` field types and validator logic.

## Minimal Example

```yaml
name: developer
description: Implements a claimed task and submits it for review

role: >
  You are a focused implementation agent working inside a Gobby-managed session.

instructions: |
  Claim the assigned task, implement the change, run targeted validation,
  then submit the task for review.

provider: inherit
isolation: worktree
timeout: 1200

workflows:
  rule_selectors:
    include:
      - "tag:default"
      - "tag:worker-safety"
  variables:
    assigned_task_id: "#123"

step_variables:
  task_claimed: false
  review_submitted: false

steps:
  - name: claim
    allowed_tools:
      - mcp__gobby__call_tool
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
    allowed_tools: "all"
    blocked_mcp_tools:
      - "gobby-tasks:close_task"
      - "gobby-agents:kill_agent"
    on_mcp_success:
      - server: gobby-tasks-ops
        tool: submit_for_review
        action: set_variable
        variable: review_submitted
        value: true
    transitions:
      - to: terminate
        when: "vars.review_submitted"

  - name: terminate
    allowed_tools:
      - mcp__gobby__call_tool
      - mcp__gobby__list_tools
      - mcp__gobby__get_tool_schema
    allowed_mcp_tools:
      - "gobby-agents:kill_agent"
```

## Inline Step Workflows

Inline `steps` are how you constrain phased agent behavior.

Each step can define:

| Field | Purpose |
| --- | --- |
| `name` | Step identifier |
| `description` | Human-readable summary |
| `status_message` | Step-specific guidance shown to the agent |
| `allowed_tools` | Allow-list of native tools, or `"all"` |
| `blocked_tools` | Explicitly blocked native tools |
| `allowed_mcp_tools` | Allow-list of MCP tools like `gobby-tasks:claim_task`, or `"all"` |
| `blocked_mcp_tools` | Explicitly blocked MCP tools |
| `on_enter` / `on_exit` | Actions to run entering or leaving the step |
| `on_mcp_success` / `on_mcp_error` | Handlers that react to MCP outcomes |
| `transitions` | Automatic transitions driven by variables |
| `exit_when` | Optional condition for leaving the step |

Rules still apply while a step workflow is active. Step restrictions are
additional guardrails, not a replacement for the rule engine.

## Isolation Modes

Isolation is a runtime choice that matters most for spawned agents.

| Isolation | Behavior | Typical use |
| --- | --- | --- |
| `none` | Work in the current repo and branch | Review, merge, or read-only helper sessions |
| `worktree` | Create or reuse a git worktree with a separate branch | Default isolated implementation flow |
| `clone` | Use a separate clone when full isolation is needed | Environments that cannot share a worktree safely |
| `inherit` | Follow the caller/runtime default | Definitions that should stay portable |

Keep provider-specific notes limited to real operational differences. For
example, some launcher environments work better with clone isolation, but that
does not change the agent model itself.

## Runtime Tools

### Definition Management (`gobby-workflows`)

Use these to inspect or change agent definitions:

- `list_agent_definitions`
- `get_agent_definition`
- `create_agent_definition`
- `toggle_agent_definition`
- `delete_agent_definition`
- `update_agent_rules`
- `update_agent_variables`
- `update_agent_steps`

### Runtime Control (`gobby-agents`)

Use these to run or coordinate agents:

- `spawn_agent`
- `dispatch_batch`
- `apply_persona`
- `list_agent_runs`
- `list_running_agents`
- `get_running_agent`
- `get_agent_result`
- `stop_agent`
- `kill_agent`

### Inter-Agent Coordination (`gobby-agents`)

Current messaging and command tools are:

- `send_message`
- `send_command`
- `activate_command`
- `complete_command`
- `deliver_pending_messages`
- `wait_for_command`
- `get_inter_session_messages`

Those are useful for orchestration flows that need parent/child coordination
without forcing everything into one monolithic prompt.

## Recommended Patterns

- Keep the definition small and declarative. Put broad invariants in rules.
- Use `workflows.rule_selectors` to opt into rule bundles by tag or name.
- Seed only the variables the agent genuinely owns.
- Use inline step workflows for lifecycle phases such as claim, implement,
  review, and terminate.
- Treat `kill_agent` as the runtime termination path for spawned workers.
- Use `apply_persona` when you want the current session to behave like an
  agent, rather than spawning yet another worker.

## Related Guides

- [Rules](./rules.md) for hook-time enforcement
- [Pipelines](./pipelines.md) for deterministic orchestration
- [Orchestration](./orchestration.md) for multi-agent coordination
