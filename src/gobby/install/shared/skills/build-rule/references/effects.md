# Rule effects

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
  `command_pattern` runs against each executable shell segment — one pipeline
  per segment (heredoc bodies whose stages are all `cat`/`tee`/`git`/`gh` or a
  bare redirection are data and excluded; bodies reaching shells, interpreters,
  or unknown tools stay in). `command_not_pattern` exempts when it matches the
  executable text as a whole.
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
  tool: memory_stats          # Tool name
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
