# Events and conditions

## Step 2: Choose Event

Ask: **"When should this rule fire?"**

Prefer normalized turn boundaries for cross-provider behavior:

- `turn_start` — once at the start of a normalized agent turn; use for per-turn
  context, reminders, and state initialization.
- `turn_end` — once when the normalized turn is ending; use for response-time
  checks and stop gates that must work across supported CLIs.

Provider and lifecycle events remain available for rules that need their narrower
semantics. The complete current `RuleTriggerEvent` surface is:

`turn_start`, `turn_end`, `session_start`, `session_end`, `setup`, `before_agent`,
`after_agent`, `stop`, `user_prompt_expansion`, `before_tool`, `after_tool`,
`before_tool_selection`, `post_tool_batch`, `before_model`, `after_model`,
`pre_compact`, `post_compact`, `subagent_start`, `subagent_stop`,
`permission_request`, `permission_denied`, `notification`, `message_display`,
`directory_added`, `stop_failure`, `task_created`, `task_completed`,
`teammate_idle`, `instructions_loaded`, `config_change`, `cwd_changed`,
`file_changed`, `worktree_create`, `worktree_remove`, `elicitation`, and
`elicitation_result`.

| Event | When It Fires | Best For |
|-------|--------------|----------|
| `turn_start` | At normalized turn start | Cross-provider per-turn guidance and state |
| `turn_end` | At normalized turn end | Cross-provider response checks and stop gates |
| `before_tool` | Before any tool call | Blocking tools, tracking tool usage |
| `after_tool` | After a tool completes | Tracking results, setting flags on success |
| `before_agent` | On provider before-agent hooks | Provider-specific agent lifecycle behavior |
| `session_start` | When session begins | Initialization, importing data |
| `session_end` | When session ends | Cleanup, exporting data |
| `stop` | On provider stop hooks | Provider-specific stop behavior |
| `pre_compact` | Before context compaction | Saving state before memory loss |

**Most common combinations:**

| Goal | Event + Effect |
|------|---------------|
| Block a tool | `before_tool` + `block` |
| Block normalized turn end | `turn_end` + `block` |
| Track tool usage | `after_tool` + `set_variable` |
| Inject context per turn | `turn_start` + `inject_context` |
| Auto-run tool at start | `session_start` + `mcp_call` |
| Save state before compact | `pre_compact` + `mcp_call` |

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
when: "task_needs_human_review(variables.get('auto_task_ref'))"

# Stop signal
when: "has_stop_signal(session_id)"

# MCP tracking
when: "mcp_called('gobby-memory', 'recall_with_synthesis')"
when: "not mcp_failed('gobby-tasks', 'validate_task')"

# Progressive discovery
when: "is_tool_unlocked(tool_input)"

# Aggregation with generators
when: "any(p in event.data.get('tool_input', {}).get('command', '') for p in ['git ', 'pytest'])"
when: "all(v > 0 for v in variables.get('scores', []))"
```

The evaluator supports: `len`, `bool`, `str`, `int`, `list`, `dict`, `any`, `all`,
plus generator expressions, list comprehensions, dict literals (`{}`), and ternary expressions.

---
