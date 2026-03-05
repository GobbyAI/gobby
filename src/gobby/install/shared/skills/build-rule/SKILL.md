---
name: build-rule
description: "Use when user asks to 'build rule', 'create rule', 'author rule', 'write rule', 'add enforcement'. Interactive guide for authoring Gobby rule YAML definitions."
version: "1.0.0"
category: authoring
triggers: build rule, create rule, author rule, write rule, add enforcement
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby build-rule — Rule Authoring Skill

Guide users through authoring Gobby rule YAML definitions. Asks what behavior to enforce, which event triggers it, what effect to apply, and generates valid YAML with proper conditions and priority.

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

## Step 2: Choose Event

Ask: **"When should this rule fire?"**

| Event | When It Fires | Best For |
|-------|--------------|----------|
| `before_tool` | Before any tool call | Blocking tools, tracking tool usage |
| `after_tool` | After a tool completes | Tracking results, setting flags on success |
| `before_agent` | Before each agent turn | Injecting context, checking messages |
| `session_start` | When session begins | Initialization, importing data |
| `session_end` | When session ends | Cleanup, exporting data |
| `stop` | When agent tries to stop | Preventing premature exit |
| `pre_compact` | Before context compaction | Saving state before memory loss |

**Most common combinations:**

| Goal | Event + Effect |
|------|---------------|
| Block a tool | `before_tool` + `block` |
| Block stop | `stop` + `block` |
| Track tool usage | `after_tool` + `set_variable` |
| Inject context per turn | `before_agent` + `inject_context` |
| Auto-run tool at start | `session_start` + `mcp_call` |
| Save state before compact | `pre_compact` + `mcp_call` |

---

## Step 3: Choose Effect

### block — Prevent an Action

```yaml
effect:
  type: block
  tools: [Edit, Write]                    # Optional: specific native tools
  mcp_tools: ["gobby-tasks:close_task"]   # Optional: specific MCP tools
  command_pattern: "git\\s+push"          # Optional: regex for Bash commands
  command_not_pattern: "git\\s+push\\s+--dry-run"  # Optional: exclude pattern
  reason: "You can't do this because..."  # Required: shown to the agent
```

**Tool matching:**
- `tools` — Native tools: `Edit`, `Write`, `Bash`, `NotebookEdit`, `mcp__gobby__call_tool`
- `mcp_tools` — MCP tools: `"server:tool"` format. Supports `"server:*"` wildcards.
- `command_pattern` / `command_not_pattern` — Only for Bash tool. Regex patterns.
- No tools/mcp_tools specified → blocks ALL tools for the event.

### set_variable — Update State

```yaml
effect:
  type: set_variable
  variable: my_flag          # Variable name
  value: true                # Literal or expression
```

**Expression detection:** If the value is a string containing `variables.`, `.get(`, `+`, `and`, `or`, `len(`, etc., it's evaluated as an expression.

```yaml
# Literal
value: true
value: 0
value: "hello"

# Expression (counter)
value: "variables.get('counter', 0) + 1"

# Expression (list append)
value: "variables.get('my_list', []) + ['new_item']"

# Expression (conditional)
value: "True if event.data.get('tool_name') == 'Edit' else False"
```

### inject_context — Add to System Message

```yaml
effect:
  type: inject_context
  template: |
    ## My Custom Guidance
    You should do X because Y.
    Current state: {{ my_variable }}
```

Templates support Jinja2: `{{ var }}`, `{{ var | default('') }}`, `{{ list | join(', ') }}`.

### mcp_call — Trigger a Tool

```yaml
effect:
  type: mcp_call
  server: gobby-memory        # MCP server name
  tool: sync_import           # Tool name
  arguments:                  # Optional args (supports {{ }} templates)
    session_id: "{{ session_id }}"
  background: true            # Optional: async execution (default: false)
```

### observe — Record an Observation

```yaml
effect:
  type: observe
  category: "tool_usage"      # Optional category (default: "general")
  message: "Tool {{ event.data.tool_name }} used"  # Optional message
```

---

## Step 4: Write Conditions

Ask: **"Should this rule always fire, or only under certain conditions?"**

### Condition Syntax

Conditions use `SafeExpressionEvaluator` — safe AST-based evaluation.

```yaml
# Simple boolean
when: "not task_claimed"

# Variable check with default
when: "variables.get('stop_attempts', 0) < 3"

# Event data check
when: "event.data.get('tool_name') == 'Bash'"

# MCP tool check
when: "event.data.get('mcp_tool') == 'close_task'"

# Tool input check
when: "'/tests/' not in tool_input.get('file_path', '')"

# Combined conditions
when: >-
  task_claimed
  and not plan_mode
  and variables.get('mode_level', 2) >= 1

# String methods
when: "tool_input.get('file_path', '').endswith('.py')"

# List membership
when: "event.data.get('source') in ['clear', 'compact']"
```

### Available in Conditions

| Variable | Available When | Description |
|----------|---------------|-------------|
| `variables` | Always | Session variables dict |
| Top-level vars | Always | Flattened session variables (e.g., `task_claimed`) |
| `event` | Always | Hook event object |
| `event.data` | Always | Event-specific data |
| `tool_input` | `before_tool`, `after_tool` | Tool arguments dict |
| `source` | `session_start` | Event source string |

### Built-in Helper Functions

```yaml
# Task helpers
when: "task_tree_complete(variables.get('session_task'))"
when: "task_needs_user_review(variables.get('auto_task_ref'))"

# Stop signal
when: "has_stop_signal(session_id)"

# MCP tracking
when: "mcp_called('gobby-memory', 'recall_with_synthesis')"
when: "not mcp_failed('gobby-tasks', 'validate_task')"

# Progressive discovery
when: "is_server_listed(tool_input)"
when: "is_tool_unlocked(tool_input)"
```

---

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
    event: stop
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
    agent_scope: [developer, expander, qa-reviewer]
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
3. **Event is valid** — One of: `before_tool`, `after_tool`, `before_agent`, `session_start`, `session_end`, `stop`, `pre_compact`.
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

## Common Patterns

### Block a specific command
```yaml
no-force-push:
  event: before_tool
  effect:
    type: block
    tools: [Bash]
    command_pattern: "git\\s+push\\s+.*--force"
    reason: "Force push is not allowed."
```

### Require a prerequisite
```yaml
require-tests-before-commit:
  event: before_tool
  when: "not variables.get('tests_passed', False)"
  effect:
    type: block
    tools: [Bash]
    command_pattern: "git\\s+commit"
    reason: "Run tests before committing."
```

### Count and gate
```yaml
count-attempts:
  event: stop
  priority: 10
  effect:
    type: set_variable
    variable: stop_attempts
    value: "variables.get('stop_attempts', 0) + 1"

block-after-threshold:
  event: stop
  priority: 50
  when: "variables.get('stop_attempts', 0) < 5"
  effect:
    type: block
    reason: "Complete your work before stopping."
```

### Inject context conditionally
```yaml
inject-tdd-reminder:
  event: before_agent
  when: "variables.get('enforce_tdd') and variables.get('task_claimed')"
  effect:
    type: inject_context
    template: |
      ## TDD Mode Active
      Write tests BEFORE implementation code.
```

### Auto-run tool on session start
```yaml
import-memories:
  event: session_start
  priority: 30
  effect:
    type: mcp_call
    server: gobby-memory
    tool: sync_import
    background: true
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
8. **Rules are templates until installed** — Files in `src/gobby/install/shared/rules/` are synced but disabled. Import and enable your custom rules.

## See Also

- [Rules Guide](docs/guides/rules.md) — Full reference
- [Variables Guide](docs/guides/variables.md) — Session variables and condition helpers
- [Workflows Overview](docs/guides/workflows-overview.md) — How rules fit with agents and pipelines
