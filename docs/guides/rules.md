# Rules

Rules are Gobby's reactive enforcement layer. They evaluate normalized hook
events and decide whether to block, rewrite, annotate, or trigger follow-up
actions. The model is **CLI agnostic**: Claude, Codex, and Gemini sessions all
feed the same rule engine once their events are normalized. The same model also
covers other supported sources as their adapters emit normalized events.

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
    effects:
      - type: block
        tools: [Bash]
        command_pattern: "git\\s+push"
        reason: "Do not push from worker sessions."
```

At sync time, each named entry under `rules:` becomes an individual rule
definition in `workflow_definitions`. Bundled YAML may still use a single
`effect` shorthand; sync wraps it into the stored `effects` array. The MCP and
HTTP authoring APIs validate the stored `effects` shape directly.

## Rule Shape

### File-Level Fields

| Field | Purpose |
| --- | --- |
| `group` | Logical grouping for related rules |
| `tags` | Discovery and selector tags applied to rules in the file |
| `sources` | Optional source metadata for installed rule rows |
| `audience` | Optional default audience for rules in the file |

### Rule-Level Fields

| Field | Purpose |
| --- | --- |
| `description` | Human-readable summary |
| `event` | Hook event that triggers the rule |
| `enabled` | Default enabled state |
| `priority` | Lower runs earlier |
| `when` | Rule-level condition |
| `match` | Accepted metadata field; current runtime filtering uses `tools`, `when`, and effect selectors |
| `tools` | Optional pre-filter on native tool name |
| `audience` | Limit the rule to `all`, `interactive`, `autonomous`, or a concrete audience |
| `agent_scope` | Limit the rule to specific agent types |
| `effects` | One or more effect definitions |

Current rules are validated as `RuleDefinitionBody`. `effects` is required in
stored definitions and must contain at least one effect. Each effect can also
have its own `when`. A rule can contain at most one `block` effect.

## Condition Expressions

Rule-level and per-effect `when` expressions are evaluated by
`SafeExpressionEvaluator`, an AST-based evaluator. Session variables are
available through `variables` and are also flattened into the top-level
context. Tool input is available as `tool_input`; for MCP `call_tool` events,
the inner `arguments` object is unwrapped while `server_name` and `tool_name`
remain available.

Supported expression features include:

- boolean logic: `and`, `or`, `not`
- comparisons: `==`, `!=`, `<`, `<=`, `>`, `>=`, `is`, `is not`, `in`, `not in`
- arithmetic: `+`, `-`, `*`, `//`, `%`
- literals: strings, numbers, booleans, `None`, lists, tuples, and dicts
- attribute and subscript access
- ternary expressions: `a if condition else b`
- list and generator comprehensions
- safe method calls on dict, str, and list values

Allowed helper functions include `len`, `bool`, `str`, `int`, `list`, `dict`,
`any`, `all`, `normalize_path`, `skill_loaded`, MCP-result helpers such as
`mcp_called` and `mcp_failed`, task helpers such as `task_state_in`, and
tool-policy helpers such as `is_discovery_tool`, `is_operator_tool`, and
`requires_task_for_any_touched_file`.

Use defensive variable access in block rules:

```yaml
when: "variables.get('task_claimed', False) and not variables.get('plan_mode')"
```

If a condition raises, block effects fail closed and fire. Other effect types
fail open and are skipped.

## Events

Rules should usually target semantic workflow events first. Raw normalized hook
events remain available as escape hatches when you need provider-specific
timing.

### Common Events

| Event | When it fires |
| --- | --- |
| `session_start` | Session bootstrap, resume, clear, or compaction re-entry |
| `turn_start` | Semantic start-of-turn boundary across supported CLIs |
| `turn_end` | Semantic end-of-turn boundary across supported CLIs |
| `before_tool` | Before a native tool or MCP tool runs |
| `after_tool` | After a tool call finishes |
| `session_end` | Session teardown |
| `task_created` | A task row has been created |
| `task_completed` | A task row has completed |
| `teammate_idle` | A teammate/agent idle signal was emitted |
| `instructions_loaded` | Runtime instructions were loaded |
| `config_change` | Configuration changed |
| `cwd_changed` | Session working directory changed |
| `file_changed` | A watched file changed |
| `worktree_create` | A worktree was created |
| `worktree_remove` | A worktree was removed |

### Raw Escape-Hatch Events

| Event | When it fires |
| --- | --- |
| `before_agent` | Raw pre-turn hook |
| `after_agent` | Raw post-turn hook |
| `stop` | Raw stop hook |
| `stop_failure` | A turn ended with an API/runtime failure |
| `pre_compact` | Before context compaction |
| `post_compact` | After context compaction, where supported |
| `before_tool_selection` | Before a model chooses tools |
| `before_model` | Before a model call |
| `after_model` | After a model call |
| `subagent_start` | Child agent starts |
| `subagent_stop` | Child agent stops |
| `permission_request` | A permission/approval request is being evaluated |
| `permission_denied` | A permission request was denied |
| `notification` | A notification-style event is emitted |
| `elicitation` | An elicitation request is being evaluated |
| `elicitation_result` | An elicitation result was received |

### About `turn_start`

`turn_start` is the portability event you usually want for prompt-entry,
turn-start context injection, and reset logic. The engine emits it alongside
the raw `before_agent` hook.

### About `turn_end`

`turn_end` is the portability event you usually want for stop gates and
turn-final checks. The engine emits it alongside the raw hook when a session
finishes a turn, so one rule can cover CLIs that surface the boundary as
`after_agent`, `stop`, or both.

`turn_end` is only the rule-authoring boundary for the current turn. Spawned
agent-run termination is a separate lifecycle action and is signaled through
`gobby-agents:end_agent_run`.

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
| `set_permission_response` | Set permission decision metadata on the hook response |
| `set_retry` | Mark an auto-denied tool call as retryable |
| `set_watch_paths` | Update dynamic file watchers |
| `set_worktree_path` | Override a generated worktree path |
| `set_elicitation` | Programmatically answer or override elicitation results |
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

For MCP `call_tool` events, rewrite updates are merged into the inner
`arguments` object so routing fields stay intact.

### Response-Metadata Effects

`set_permission_response`, `set_retry`, `set_watch_paths`, `set_worktree_path`,
and `set_elicitation` write response metadata consumed by hook adapters or
runtime handlers. Use these only when the hook surface expects that metadata.

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
4. Filter by audience and the session's active rule selectors.
5. Run hard-coded agent and step tool enforcement for `before_tool`.
6. Evaluate each rule's `tools`, `when`, and effect selectors.
7. Apply matching non-block effects in order, then apply a deferred block if present.
8. Stop rule evaluation at the first matching block.

Important runtime semantics:

- `set_variable` effects are visible to later rules in the same pass.
- `inject_context` effects accumulate.
- `mcp_call` effects are collected and dispatched after evaluation.
- Inline `mcp_call` effects with `inject_result` can inject formatted results and stop sibling effects on failure.
- In a multi-effect rule, non-block effects run before the rule's block effect.
- Rule conditions skip rules; they do not stop evaluation.
- Some universal safety behavior is hard-coded in the engine, not expressed in YAML.
- On `turn_start`, the engine resets transient stop/tool-block state and may seed progressive MCP discovery.
- On `turn_end`, the engine increments `stop_attempts` before configurable rules run.

## Activation Model

A rule only affects a session when all of the following are true:

1. The definition exists in `workflow_definitions`.
2. The rule is enabled.
3. Session overrides have not disabled it.
4. Its `agent_scope`, if present, matches the session's agent type.
5. Its `audience`, if present, matches the current runtime audience.
6. The current session's active rule selectors include it.

That means bundled YAML is only the template source. The database plus active
selectors determine what actually runs.

## Public Tooling

Use the `gobby-workflows` MCP server to manage standalone rules:

- `list_rules`
- `get_rule`
- `create_rule`
- `update_rule`
- `toggle_rule`
- `delete_rule`

The CLI also exposes operator commands under `gobby rules`, including `list`,
`show`, `enable`, `disable`, `import`, `export`, and `audit`.

For authoring caveats and engine behavior that matters when designing rules,
see [Rule Authoring Guide](./workflow-rules.md).

_Last verified: 2026-05-07_
