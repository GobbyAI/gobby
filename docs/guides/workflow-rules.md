# Rule Authoring Guide

This guide covers the engine behaviors that matter when writing rules. For the
full reference for fields, events, and effects, see [rules.md](./rules.md).

## Variable Safety In `when`

Rule conditions are evaluated against session variables. If you reference a
missing variable directly, the expression can raise `NameError`.

That matters most for `block` rules because a condition failure there can fail
closed and block the session unexpectedly.

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

## Prefer `turn_end` For End-Of-Turn Policy

If a rule is intended to run when a turn finishes, prefer `turn_end` over a
single raw hook such as `stop` or `after_agent`.

Why:

- `turn_end` is the cross-CLI semantic event.
- The rule engine emits it alongside raw stop/after-agent boundaries.
- One rule then covers Claude, Codex, and Gemini session endings consistently.

Use raw `stop` or `after_agent` only when you truly need the distinction.

## Hard-Coded Engine Behaviors

Some safety behaviors live in the rule engine itself rather than YAML.

### Consecutive Tool-Block Escalation

If the same tool is blocked repeatedly without recovery:

- the engine tracks the blocked tool identity
- retries of the same tool increment a counter
- repeated retries eventually trigger a stronger hard-coded block

This is meant to break bad retry loops and push the agent toward a different
recovery action.

### Tool-Block Stop Gate

If a tool just failed or was blocked and the session immediately tries to stop,
the engine can block the stop once and force recovery first.

### Catastrophic Failure Escape Hatch

Certain fatal provider/account errors set `force_allow_stop` so the next stop
attempt is allowed unconditionally.

### Stop Attempt Counting

`stop_attempts` is incremented automatically on `turn_end`, before configurable
stop-gate rules run.

### Before-Agent Reset

On `before_agent`, the engine resets transient stop-cycle state such as:

- `consecutive_tool_blocks`
- `_last_blocked_tool`
- `tool_block_pending`
- `stop_attempts`

## Practical Advice

- Use `variables.get()` defensively in `block` rules.
- Keep rule conditions cheap and explicit.
- Prefer multi-effect rules when a block, rewrite, and context injection are
  all part of one policy.
- Use `turn_end` for cross-CLI end-of-turn behavior.
- Treat hard-coded engine safety as part of the contract when debugging rule
  interactions.
