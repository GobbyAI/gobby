# Conditions

Load before writing or debugging `when`. Read `get_rule` and use actual event
data and registered helpers, not arbitrary Python calls. The AST-based
`SafeExpressionEvaluator` restricts operations and calls. Keep guards cheap.

Use `variables.get('flag', False)` for optional state. Variables also appear at
top level, but missing bare names raise. Context supplies `event`, `variables`,
`tool_input`, `source`, and project data. For Gobby `call_tool`, `tool_input`
exposes inner arguments plus outer `server_name` and `tool_name`; `event.data`
retains the normalized envelope.

```yaml
when: >-
  variables.get('task_claimed', False)
  and tool_input.get('server_name') == 'gobby-tasks'
  and tool_input.get('tool_name') == 'close_task'
```

Boolean/comparison/arithmetic expressions, literals, safe method calls,
comprehensions, ternaries, and registered helpers are supported. Check current
helper signatures in evaluator/context builders; do not copy retired examples.

Rule-level errors fail closed if any block exists, so eligible non-block siblings
can also run. Non-block-only rules fail open. Per-effect errors independently
fail closed for `block` and skip other effects. Database cancellation/deadline
errors propagate. Effect selectors still apply after a rule condition error.

Later rules receive refreshed context after in-place variable changes. Within
one rule, prefer `variables.get(...)` after a sibling mutation: flattened scalar
aliases were captured when its context was built. A block condition is checked
when encountered; the selected block's application is deferred.

Recover unknown-name/type errors with defensible defaults or correct payload
access. Test missing and present values plus malformed input in isolation;
never weaken a guard simply to hide its error.

Guide: [Conditions](../../../../../../../../docs/guides/rules.md#condition-expressions).

_Last verified: 2026-09-12_
