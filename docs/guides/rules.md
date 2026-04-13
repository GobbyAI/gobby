# Rules

Rules are Gobby's reactive enforcement layer. They evaluate normalized hook
events and decide whether to block, rewrite, annotate, or trigger follow-up
actions. The model is **CLI agnostic**: Claude, Codex, and Gemini sessions all
feed the same rule engine once their events are normalized.

For the larger system model, see [Workflows Overview](./workflows-overview.md).

## What Rules Are Good For

Use rules when you need behavior that should happen automatically at hook time:

- block a tool call or stop attempt
- rewrite unsafe or non-compliant tool input
- inject dynamic context into the next turn
- seed or mutate session variables
- trigger MCP side effects when a condition becomes true
- load a skill automatically for a specific workflow state

Rules are not the right tool for long-running control flow. Use pipelines or
agent step workflows for that.

## YAML Format

Bundled and project rule files are grouped YAML documents:

```yaml
group: worker-safety
tags: [default, safety]

rules:
  no-push:
    description: "Block git push from worker sessions"
    event: before_tool
    priority: 50
    when: "variables.get('_agent_type') is not None"
    effect:
      type: block
      tools: [Bash]
      command_pattern: "git\\s+push"
      reason: "Do not push from worker sessions."
```

At sync time, each named entry under `rules:` becomes an individual rule
definition in `workflow_definitions`.

## Rule Shape

### File-Level Fields

| Field | Purpose |
| --- | --- |
| `group` | Logical grouping for related rules |
| `tags` | Discovery and selector tags applied to rules in the file |

### Rule-Level Fields

| Field | Purpose |
| --- | --- |
| `description` | Human-readable summary |
| `event` | Hook event that triggers the rule |
| `enabled` | Default enabled state |
| `priority` | Lower runs earlier |
| `when` | Rule-level condition |
| `match` | Optional exact-match filter on normalized event data |
| `agent_scope` | Limit the rule to specific agent types |
| `effect` | Single effect definition |
| `effects` | Multi-effect form; use this instead of `effect` |

Current rules are validated as `RuleDefinitionBody`. In multi-effect rules,
each effect can also have its own `when`.

## Events

Rules can target both raw hook events and the semantic `turn_end` event.

### Common Events

| Event | When it fires |
| --- | --- |
| `session_start` | Session bootstrap, resume, clear, or compaction re-entry |
| `before_agent` | Before the next model/agent turn is prepared |
| `before_tool` | Before a native tool or MCP tool runs |
| `after_tool` | After a tool call finishes |
| `turn_end` | Semantic end-of-turn event for stop/after-agent flows |
| `session_end` | Session teardown |

### Additional Runtime Events

| Event | When it fires |
| --- | --- |
| `after_agent` | Raw post-turn hook |
| `stop` | Raw stop hook |
| `pre_compact` | Before context compaction |
| `before_tool_selection` | Before a model chooses tools |
| `before_model` | Before a model call |
| `after_model` | After a model call |
| `subagent_start` | Child agent starts |
| `subagent_stop` | Child agent stops |
| `permission_request` | A permission/approval request is being evaluated |
| `notification` | A notification-style event is emitted |

### About `turn_end`

`turn_end` is the portability event you usually want for stop gates and
turn-final checks. The engine emits it alongside the raw hook when a session
finishes a turn, so one rule can cover CLIs that surface the boundary as
`after_agent`, `stop`, or both.

## Effects

Gobby currently supports these effect types:

| Effect | Purpose |
| --- | --- |
| `block` | Prevent the action and return a reason |
| `set_variable` | Update session state in-place |
| `inject_context` | Append text to the session context |
| `mcp_call` | Queue an MCP call as part of rule evaluation |
| `observe` | Append structured observations to session state |
| `rewrite_input` | Modify tool input before execution |
| `load_skill` | Resolve a skill and inject its contents into context |

## Effect Notes

### `block`

Use this to stop a tool call, stop attempt, or other action.

Supported match fields include:

- `tools`
- `mcp_tools`
- `command_pattern`
- `command_not_pattern`

Only one `block` effect is allowed per rule. First matching block wins.

### `set_variable`

`set_variable` mutates the session variables immediately. Later rules in the
same evaluation pass see the updated value.

### `inject_context`

Multiple `inject_context` effects accumulate. This is how rule bundles append
reminders, handoff text, or recovery guidance without replacing the whole
system prompt.

### `mcp_call`

Use `mcp_call` when a hook needs to trigger a tool automatically. Current
effect options include:

- `background`
- `inject_result`
- `block_on_failure`
- `block_on_success`

### `rewrite_input`

`rewrite_input` changes the pending tool input before it runs. Current bundled
rules use this for behaviors such as forcing `uv run` or stripping disallowed
flags.

### `load_skill`

`load_skill` resolves a Gobby-managed skill and injects it into the session
context. This keeps skill activation in the workflow layer rather than
hard-coding it into a single CLI.

## Example: Multi-Effect Rule

```yaml
group: tool-hygiene

rules:
  require-uv:
    event: before_tool
    when: "event.data.get('tool_name') == 'Bash'"
    effects:
      - type: rewrite_input
        input_updates:
          command: >-
            {{
              event.data.get('command', '')
              | regex_replace('^python\\s+', 'uv run python ')
            }}
      - type: inject_context
        template: "Prefer `uv run ...` for Python commands in this repo."
```

## Evaluation Rules

The engine evaluates rules like this:

1. Resolve raw and semantic events for the incoming hook.
2. Load enabled rules for those events.
3. Apply session overrides and agent-scope filtering.
4. Filter by the session's active rule selectors.
5. Sort by priority.
6. Evaluate each rule's `when` and `match`.
7. Apply effects in order until evaluation completes or a block returns.

Important runtime semantics:

- `set_variable` effects are visible to later rules in the same pass.
- `inject_context` effects accumulate.
- `mcp_call` effects are collected and dispatched after evaluation.
- Rule conditions skip rules; they do not stop evaluation.
- Some universal safety behavior is hard-coded in the engine, not expressed in YAML.

## Activation Model

A rule only affects a session when all of the following are true:

1. The definition exists in `workflow_definitions`.
2. The rule is enabled.
3. The current session's agent/persona selectors include it.
4. Its `agent_scope`, if present, matches the session's agent type.

That means bundled YAML is only the template source. The database plus active
selectors determine what actually runs.

## Public Tooling

Use `gobby-workflows` to manage standalone rules:

- `list_rules`
- `get_rule`
- `create_rule`
- `update_rule`
- `toggle_rule`
- `delete_rule`

For authoring caveats and engine behavior that matters when designing rules,
see [Rule Authoring Guide](./workflow-rules.md).
