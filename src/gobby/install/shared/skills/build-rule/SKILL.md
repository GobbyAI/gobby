---
name: build-rule
description: "Use when authoring or updating a Gobby rule definition."
version: "1.0.1"
category: authoring
triggers: build rule, create rule, author rule, write rule, add enforcement
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby build-rule — Rule Authoring Skill

## Workflow Overview

1. **Identify Behavior** — What should be enforced or tracked?
2. **Choose Event** — When should the rule fire?
3. **Choose Effect** — What should happen?
4. **Write Conditions** — When should the effect apply?
5. **Generate YAML** — Produce the rule definition
6. **Validate & Install** — Check and import

---

## Step 1: Identify Behavior

Ask the user:

1. **"What behavior do you want to enforce or track?"** — One sentence.
2. **"Should it block an action, track state, inject guidance, call a tool, or observe?"**

Common behaviors and their effect types:

| Behavior | Effect Type |
|----------|-------------|
| Prevent dangerous commands | `block` |
| Require a prerequisite before an action | `block` |
| Track what the agent has done | `set_variable` |
| Count occurrences of something | `set_variable` |
| Add guidance to the system message | `inject_context` |
| Auto-run a tool at session start | `mcp_call` |
| Log tool usage for analytics | `observe` |

---

## Conditional Detail

- To select an event or author a condition, call `get_skill_file(name="build-rule", path="references/events-and-conditions.md")`.
- To choose or configure an effect, call `get_skill_file(name="build-rule", path="references/effects.md")`.
- For reusable examples after the event and effect are known, call `get_skill_file(name="build-rule", path="references/common-patterns.md")`.

A normal authoring pass loads the event/condition and effect references; load patterns only when the rule does not follow the common path.

## Step 5: Generate YAML

### Single Rule

```yaml
group: my-custom-rules
tags: [custom]

rules:
  my-rule-name:
    description: "What this rule does"
    event: before_tool
    priority: 100
    when: "condition expression"
    effect:
      type: block
      tools: [Bash]
      reason: "Why this is blocked"
```

### Multi-Effect Rule

```yaml
group: my-custom-rules
tags: [custom]

rules:
  my-complex-rule:
    description: "Track and block in one rule"
    event: before_tool
    priority: 30
    when: "base condition"
    effects:
      - type: set_variable
        variable: attempt_count
        value: "variables.get('attempt_count', 0) + 1"

      - type: inject_context
        when: "variables.get('attempt_count', 0) > 2"   # Per-effect condition
        template: "You've attempted this {{ attempt_count }} times."

      - type: block
        tools: [Bash]
        reason: "Blocked after too many attempts."
```

### Multiple Rules in One Group

```yaml
group: my-enforcement
tags: [custom, enforcement]

rules:
  track-edits:
    description: "Track file edits"
    event: after_tool
    when: "event.data.get('tool_name') in ['Edit', 'Write']"
    effect:
      type: set_variable
      variable: files_edited
      value: "variables.get('files_edited', 0) + 1"

  require-commit:
    description: "Require commit before stop if files were edited"
    event: turn_end
    when: "variables.get('files_edited', 0) > 0 and not variables.get('committed', False)"
    effect:
      type: block
      reason: "You edited {{ files_edited }} files. Commit before stopping."
```

### Priority Guidelines

| Range | Purpose |
|-------|---------|
| 5–10 | State initialization (counters, flags) |
| 10–20 | Primary blocking gates |
| 20–30 | Secondary enforcement |
| 30–50 | Tracking and TDD |
| 50+ | Context injection and MCP calls |
| 100 | Default (most custom rules) |

### agent_scope

Scope rules to specific agent types:

```yaml
rules:
  no-push-workers:
    event: before_tool
    agent_scope: [backend-developer, merge-worker, qa-reviewer]
    effect:
      type: block
      tools: [Bash]
      command_pattern: "git\\s+push"
      reason: "Workers don't push."
```

---

## Step 6: Validate & Install

### Validation Checklist

1. **Group name is kebab-case** — `my-rules`, not `myRules`.
2. **Rule names are kebab-case** — `no-push`, not `noPush`.
3. **Event is valid** — It appears in the complete current `RuleTriggerEvent` list in Step 2; prefer `turn_start` and `turn_end` for normalized turn behavior.
4. **Effect type is valid** — One of: `block`, `set_variable`, `inject_context`, `mcp_call`, `observe`.
5. **Block effects have `reason`** — Required field.
6. **set_variable effects have `variable` and `value`** — Both required.
7. **inject_context effects have `template`** — Required field.
8. **mcp_call effects have `server` and `tool`** — Both required.
9. **Conditions use correct syntax** — `variables.get('key', default)` not `variables['key']`.
10. **Regex patterns are properly escaped** — `\\s+` not `\s+` (YAML double-escaping).
11. **Multi-effect rules use `effects` (plural)** — Not `effect` with a list.
12. **At most one block per rule** — Multi-effect rules can only have one block effect.
13. **Block effects match the event** — `tools` matching is only useful for `before_tool`.

### Install

```bash
# Import from YAML file
gobby rules import my-rules.yaml
```

Or via MCP:
```python
# Get the schema first
get_tool_schema("gobby-workflows", "create_rule")

# Create the rule
call_tool("gobby-workflows", "create_rule", {
    "name": "my-rule",
    "group": "my-group",
    "definition": { ... }
})
```

Tell the user:
```
Rule installed! To verify:

  gobby rules list --group my-group
  gobby rules show my-rule-name

To test, trigger the event and check the audit log:
  gobby rules audit --limit 5
```

---

## Key Gotchas

1. **First block wins** — If multiple rules match, only the first block (by priority) fires. Other rules after it don't run.
2. **Block effects fail closed** — If the condition errors, a block effect defaults to BLOCKING. Be careful with complex conditions.
3. **Other effects fail open** — If the condition errors, non-block effects are SKIPPED.
4. **Variables mutate in-place** — A `set_variable` in rule 1 (priority 10) is visible to rule 2 (priority 20) in the same evaluation pass.
5. **YAML regex needs double escaping** — `\\s+` in YAML becomes `\s+` in the regex engine.
6. **Templates are Jinja2** — `{{ var }}` in `reason` and `template` fields. Use `{{ var | default('') }}` for safety.
7. **`mcp_tools` uses `"server:tool"` format** — Not just the tool name.
8. **Rules are templates until installed** — Bundled rule groups in `src/gobby/install/shared/workflows/rules/` sync to the DB registry on startup with the template's `enabled` value (enabled by default). The DB row is the source of truth — import and enable your custom rules; check the installed row, not the YAML, to see what's active.

## See Also

- [Rules Guide](docs/guides/rules.md) — Full reference
- [Variables Guide](docs/guides/variables.md) — Session variables and condition helpers
- [Workflows Overview](docs/guides/workflows-overview.md) — How rules fit with agents and pipelines
