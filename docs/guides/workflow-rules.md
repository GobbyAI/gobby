# Rule Authoring Guide

This guide covers the engine behaviors that matter when writing rules. For the
full reference for fields, events, and effects, see [rules.md](./rules.md).

Gobby 0.4.0 treats `turn_start` and `turn_end` as the primary rule-authoring
events for agent turns. Raw events such as `before_agent`, `after_agent`, and
`stop` are normalized provider/runtime details that remain available for
escape-hatch rules.

## Variable Safety In `when`

Rule conditions are evaluated against session variables. If you reference a
missing variable directly, the expression can raise `NameError`.

That matters most for `block` rules because a condition failure there can fail
closed and block the session unexpectedly.

Non-block effects fail open on condition errors and do not fire. `block`
effects fail closed because the engine prefers a conservative safety block over
silently allowing an action when the guard cannot be evaluated.

### Risky Pattern

```yaml
when: "task_claimed and some_other_condition"
```

### Safe Pattern

```yaml
when: "variables.get('task_claimed', False) and some_other_condition"
```

### When To Seed A Default

If a variable is used broadly, initialize it in a bundled or project variable
definition or with an early `session_start` rule instead of repeating
`variables.get(...)` everywhere.

Example path in the current bundled layout:

```text
src/gobby/install/shared/workflows/rules/...
```

## Installed Rules Are The Source Of Truth

Bundled YAML files are templates. They become active only after they are synced
into the rules database and selected for the current session.

Before telling a user that a rule is enabled, disabled, or absent, inspect the
installed rule state through `gobby-workflows` or the backing database. A YAML
file can exist while the installed definition is disabled, filtered out by the
session's active selectors, or shadowed by a session override.

## Author Against Semantic Turn Events

The rule engine resolves one incoming raw hook into one or more rule events. A
raw turn-start hook also evaluates `turn_start` rules; raw turn-end hooks also
evaluate `turn_end` rules.

```mermaid
flowchart LR
    before[before_agent] --> start[turn_start rules]
    after[after_agent] --> end[turn_end rules]
    stop[stop] --> end
    stopFailure[stop_failure] --> end
```

### Prefer `turn_end` For End-Of-Turn Policy

If a rule is intended to run when a turn finishes, prefer `turn_end` over a
single raw hook such as `stop`, `after_agent`, or `stop_failure`.

Why:

- `turn_end` is the portable semantic event for end-of-turn gates.
- The rule engine evaluates it alongside raw stop/after-agent boundaries.
- One rule can cover CLIs that surface the boundary differently.

Use raw `stop`, `after_agent`, or `stop_failure` only when the rule depends on
provider-specific timing or payload shape.

### Prefer `turn_start` For Start-Of-Turn Policy

If a rule is intended to run when a new turn begins, prefer `turn_start` over
the raw `before_agent` hook.

Why:

- `turn_start` is the portable semantic start-of-turn event.
- The rule engine evaluates it alongside the raw pre-turn hook.
- One rule can cover prompt-entry, context-injection, and reset logic across
  supported CLIs.

Use raw `before_agent` only when you truly need provider-specific detail.

### Keep Agent Termination Separate

`turn_end` is a rule-evaluation boundary. It is not the same thing as ending a
workflow agent process.

Workflow agents that complete their stage still need to call the
`gobby-agents:end_agent_run` MCP tool when their agent definition says to
terminate. That lifecycle call releases the running agent; it does not replace
`turn_end` rules, and `turn_end` rules do not replace it.

## Hard-Coded Engine Behaviors

Some safety behaviors live in the rule engine itself rather than YAML.

### Consecutive Tool-Block Escalation

If the same tool is blocked repeatedly without recovery:

- the engine tracks the blocked tool identity
- retries of the same tool increment a counter
- repeated retries eventually trigger a stronger hard-coded block

This is meant to break bad retry loops and push the agent toward a different
recovery action.

For MCP calls routed through `call_tool`, the tracked identity is
`server:tool`. A repeated block on `gobby-tasks:close_task` does not make every
other MCP tool look like the same failed retry.

### Tool-Block Stop Gate

If a tool fails and the session immediately tries to end the turn, the engine
can block once with `tool-failure-recovery` and clear `tool_block_pending`.

If a write-like edit is pending after a failed or in-flight mutation, the engine
can block turn end with `edit-write-recovery`. A small circuit breaker prevents
that hard-coded gate from looping forever.

### Catastrophic Failure Escape Hatch

Certain fatal provider/account errors set `force_allow_stop` so the next stop
attempt can bypass the tool-failure recovery gate. Claimed-task gates still win:
if `task_claimed` is true, the engine suppresses the force-allow and leaves
task-close rules in control.

### Stop Attempt Counting

`stop_attempts` is incremented automatically on `turn_end`, before configurable
stop-gate rules run. Bundled stop-gate rules pair it with `max_stop_attempts`
to prevent permanent stop blocking.

### Turn-Start Reset

On `turn_start`, the engine resets transient stop-cycle state such as:

- `consecutive_tool_blocks`
- `_last_blocked_tool`
- `tool_block_pending`
- `stop_attempts`
- `_block_reasons_shown`

On the first turn where `servers_listed` is false, the engine also queues the
hard-coded auto-discovery MCP call that seeds available Gobby MCP servers.

The reset is per turn, not per process. It does not close a task, release an
agent run, or clear long-lived workflow state.

### Multi-Effect Ordering

For a matching rule, the engine applies non-block effects before the block
effect. That lets one rule set variables, inject context, or queue MCP calls and
then block the event with a rendered reason. Per-effect `when` conditions are
evaluated separately from the rule-level `when`.

## Practical Advice

- Use `variables.get()` defensively in `block` rules.
- Keep rule conditions cheap and explicit.
- Prefer multi-effect rules when a block, rewrite, and context injection are
  all part of one policy.
- Verify installed rule state, not just bundled YAML templates, when debugging
  whether a rule is active.
- Use `turn_start` for portable prompt-entry and reset behavior.
- Use `turn_end` for cross-CLI end-of-turn behavior.
- Treat raw lifecycle events as escape hatches for provider/runtime-specific
  behavior.
- Call `end_agent_run` when a workflow agent reaches its terminate step; do not
  model termination as a rule event.
- Treat hard-coded engine safety as part of the contract when debugging rule
  interactions.

_Last verified: 2026-05-07_
