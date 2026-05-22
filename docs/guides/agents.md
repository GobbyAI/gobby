# Agents

Agents are workflow definitions that describe either a current-session persona or
a spawned worker session. The same definition model works across supported CLIs;
provider-specific hooks are normalized before workflow rules evaluate.

For the broader control-plane model, see [Workflows Overview](./workflows-overview.md).

## Usage Surfaces

Agent definitions have explicit `surfaces`:

| Surface | Runtime tool | What happens |
| --- | --- | --- |
| `persona` | `gobby-agents:apply_persona` | Updates the current session's persona and active skill selection for the next user turn. |
| `spawn` | `gobby-agents:spawn_agent` or `dispatch_batch` | Starts a child session, records an agent run, and optionally creates or reuses isolation. |

`apply_persona` is intentionally narrow. It sets prompt-facing persona state,
skill selection, and reinjection flags; it does not change provider, model,
isolation, active rules, tool restrictions, or inline step workflow state.

Spawned runs use the full runtime path. They can inherit or override execution
settings, register inline step workflows, receive task/session variables, and
publish completion state back to waiting parents.

## Definition Storage

Agent definitions are stored in `workflow_definitions` with
`workflow_type='agent'` and are managed through `gobby-workflows`.

Bundled definitions live in:

```text
src/gobby/install/shared/workflows/agents/
```

The bundled directory includes enabled definitions for planning, review,
writing, analysis, image generation, maintenance, merge work, and default
interactive use. It also includes disabled deprecated tombstones under
`deprecated/`; those files preserve migration history and should not be used as
current agent names.

Use these tools to inspect or change definitions:

- `gobby-workflows:list_agent_definitions`
- `gobby-workflows:get_agent_definition`
- `gobby-workflows:create_agent_definition`
- `gobby-workflows:toggle_agent_definition`
- `gobby-workflows:delete_agent_definition`
- `gobby-workflows:update_agent_rules`
- `gobby-workflows:update_agent_variables`
- `gobby-workflows:update_agent_steps`

## Definition Shape

The current `AgentDefinitionBody` schema accepts these primary fields:

| Field | Purpose |
| --- | --- |
| `name` | Unique definition name |
| `description` | Human-readable summary |
| `sources` | Optional CLI-source filter |
| `surfaces` | `spawn`, `persona`, or both |
| `role` / `goal` / `personality` / `instructions` | Prompt blocks used for persona or run context |
| `provider` | Provider override or `inherit` |
| `model` | Optional model override |
| `reasoning_effort` | Optional normalized reasoning effort string |
| `reasoning_required` | Whether unsupported reasoning should fail instead of warn |
| `fallback_agent` | Optional fallback definition for provider rotation |
| `api_base` / `api_token` | Optional custom model endpoint configuration |
| `isolation` | `none`, `worktree`, `clone`, or `inherit` |
| `base_branch` | Branch used for new isolation, or `inherit` |
| `timeout` / `max_turns` | Runtime limits; `0` means unlimited |
| `workflows` | Rule, skill, variable, and pipeline selectors |
| `skills` | Metadata for baseline and allow-listed skill families |
| `blocked_tools` / `blocked_mcp_tools` | Definition-level restrictions |
| `steps` | Optional inline step workflow |
| `step_variables` | Initial variables for inline steps |
| `exit_condition` | Workflow-level completion condition |
| `enabled` / `deprecated` / `deprecated_reason` | Definition lifecycle metadata |

Older YAML may contain a `mode` field. The schema ignores extra fields for
compatibility, but new definitions should use `surfaces` plus the runtime tool
choice instead.

## Strict Execution Fields

Several execution fields use strict YAML types:

- `model`
- `reasoning_effort`
- `reasoning_required`
- `fallback_agent`
- `api_base`
- `api_token`

Invalid:

```yaml
model: 1234
reasoning_effort: 2
reasoning_required: "false"
fallback_agent: 0
api_base: 12345
api_token: false
```

Valid:

```yaml
model: "gpt-5.5"
reasoning_effort: high
reasoning_required: false
fallback_agent: "qa-reviewer"
api_base: "http://localhost:1234/v1"
api_token: "${LM_STUDIO_API_KEY}"
```

`reasoning_effort` is normalized by the agent reasoning layer. Use the same
strings accepted by the current provider/model routing code.

## Minimal Definition

```yaml
name: docs-worker
description: Documentation implementation worker
surfaces: [spawn, persona]
provider: inherit
isolation: inherit
timeout: 1200
max_turns: 0

role: >
  You write and verify Gobby documentation against the local source tree.

instructions: |
  Claim the assigned docs task, audit the guide against code-owned sources,
  make a scoped docs change, run focused verification, commit, and hand off.

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
    allowed_tools: "all"
    blocked_mcp_tools:
      - "gobby-tasks:reopen_task"
    on_mcp_success:
      - server: gobby-tasks-ops
        tool: submit_for_review
        action: set_variable
        variable: review_submitted
        value: true
    transitions:
      - to: finish
        when: "vars.review_submitted"

  - name: finish
    allowed_tools:
      - mcp__gobby__call_tool
      - mcp__gobby__list_mcp_servers
      - mcp__gobby__list_tools
      - mcp__gobby__get_tool_schema
    allowed_mcp_tools:
      - "gobby-agents:end_agent_run"
```

## Inline Step Workflows

Inline `steps` constrain phased behavior for spawned runs. Each step can define:

| Field | Purpose |
| --- | --- |
| `name` | Step identifier |
| `description` | Human-readable summary |
| `status_message` | Step-specific guidance shown to the session |
| `allowed_tools` / `blocked_tools` | Native tool restrictions |
| `allowed_mcp_tools` / `blocked_mcp_tools` | MCP tool restrictions such as `gobby-tasks:claim_task` |
| `on_enter` / `on_exit` | Actions around step boundaries |
| `on_mcp_before` / `on_mcp_success` / `on_mcp_error` | Handlers for MCP attempts or outcomes |
| `transitions` | Variable-driven step transitions |
| `exit_when` | Optional per-step exit condition |

Step restrictions are additive with the rule engine. A tool must satisfy both
the current step and the active rules.

## Lifecycle Model

Rules should be authored against semantic workflow events:

- `turn_start`
- `turn_end`

Raw `before_agent`, `after_agent`, and `stop` events are normalized runtime
details. Use them only when the distinction is the subject of the rule. In the
workflow engine, `turn_start` resolves from the pre-turn boundary, while
`turn_end` resolves from post-turn and stop boundaries.

Ending a chat turn is separate from ending a spawned agent run. A spawned worker
that has completed its workflow should call `gobby-agents:end_agent_run` so the
run is marked successful and completion subscribers are notified.

Use `gobby-agents:stop_agent` when a parent wants to cancel a pending or running
run. Use `gobby-agents:kill_agent` for targeted process termination and runtime
cleanup. `kill_agent` can also self-terminate with a status, but normal workflow
success should use `end_agent_run`.

## Runtime Tools

`gobby-agents` owns run execution, inspection, termination, persona application,
messaging, and command coordination.

Run tools:

- `spawn_agent`
- `dispatch_batch`
- `apply_persona`
- `get_agent_result`
- `list_agent_runs`
- `list_running_agents`
- `get_running_agent`
- `stop_agent`
- `kill_agent`
- `end_agent_run`
- `can_spawn_agent`
- `evaluate_spawn`
- `running_agent_stats`
- `unregister_agent`

Coordination tools:

- `send_message`
- `send_command`
- `activate_command`
- `complete_command`
- `deliver_pending_messages`
- `wait_for_command`
- `get_inter_session_messages`

`send_message` uses explicit targets: `session`, `agent`, `project`, `build`,
or `all`. Pass `target_id` for every target except `all`; `session` accepts a
session ref, `agent` an agent run id, `project` a project id/name, and `build`
a build run id, build input ref, or root task ref.

Spawn requests can pass `agent`, `task_id`, isolation fields, provider/model
overrides, reasoning fields, runtime limits, parent session, and project path.
`dispatch_batch` uses the same spawn machinery for multiple task suggestions.

## Isolation

Isolation is a runtime setting for spawned runs:

| Isolation | Behavior |
| --- | --- |
| `none` | Work in the caller's current repository context |
| `worktree` | Create or reuse a git worktree with separate branch state |
| `clone` | Use a separate clone for stronger filesystem isolation |
| `inherit` | Defer to caller/runtime defaults |

Docs leaf work may run inside a parent epic's existing isolation context. In
that case the agent definition can keep `isolation: inherit`, and dispatch
provides the concrete worktree or clone context.

## Recommended Patterns

- Put reusable safety policy in rules; keep agent definitions focused on role,
  selectors, runtime settings, and step flow.
- Use `surfaces` to make persona-capable definitions explicit.
- Seed only variables the agent owns, such as `assigned_task_id` or
  stage-specific gates.
- Use inline steps for lifecycle phases: claim, load required skills,
  implement, review handoff, finish.
- Use `end_agent_run` as the explicit successful termination path for spawned
  workflows.
- Treat deprecated tombstones as migration records, not runnable definitions.

## Related Guides

- [Workflow Rules](./workflow-rules.md) for semantic rule events
- [Rules](./rules.md) for hook-time enforcement
- [Pipelines](./pipelines.md) for deterministic automation
- [Orchestration](./orchestration.md) for stage dispatch and review flow

_Last verified: 2026-05-07_
