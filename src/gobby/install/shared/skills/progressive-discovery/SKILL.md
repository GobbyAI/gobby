---
name: progressive-discovery
description: "Progressive discovery pattern for MCP tools and skills. Teaches agents just-in-time discovery to minimize token usage."
category: core
alwaysApply: false
injectionFormat: content
metadata:
  gobby:
    audience: all
    format_overrides:
      autonomous: full
---

## Skills & Discovery

**Skills** — context-aware prompts for common workflows.
- Browse: `list_skills()` on `gobby-skills`
- Load: `get_skill(name="skillname")` on `gobby-skills`
- Search: `search_skills(query="topic")` on `gobby-skills`
- Hubs: `search_hub(query)` on `gobby-skills`

**MCP Tools** — progressive discovery keeps token usage low.

Internal servers (gobby-tasks, gobby-memory, etc.) are pre-discovered.
Skip straight to the tool you need:
1. `get_tool_schema(server_name, tool_name)` — get full inputSchema (recommended before first call)
2. `call_tool(server_name, tool_name, arguments)` — execute

External MCP servers require full discovery:
1. `list_mcp_servers()` — discover servers (~50 tokens)
2. `list_tools(server_name)` — discover tools (~100 tokens per server)
3. `get_tool_schema(server_name, tool_name)` — get full inputSchema
4. `call_tool(server_name, tool_name, arguments)` — execute

**Core principle:** NEVER load all schemas upfront — 50+ tool schemas consume 30-40K tokens. Fetch schemas only when you're about to call a tool.

## Common Mistakes

```python
# WRONG — Loading everything upfront (wastes 30-40K tokens)
for server in servers:
    for tool in list_tools(server):
        get_tool_schema(server, tool)  # Unnecessary!

# RIGHT — Just-in-time discovery
get_tool_schema("gobby-tasks", "create_task")  # Learn: needs "title" not "name"
call_tool("gobby-tasks", "create_task", {"title": "Fix bug", "session_id": "#123"})

# ALSO OK — Direct call when you know the params
call_tool("gobby-tasks", "create_task", {"title": "Fix bug", "session_id": "#123"})
# If params are wrong, the error includes the full schema for self-correction
```

## Available Internal Servers

| Server | Purpose |
|--------|---------|
| `gobby-tasks` | Task management |
| `gobby-sessions` | Session handoff |
| `gobby-memory` | Persistent memory |
| `gobby-workflows` | Workflow control |
| `gobby-canvas` | Canvas / collaboration |
| `gobby-metrics` | Usage metrics |
| `gobby-agents` | Agent spawning |
| `gobby-worktrees` | Git worktrees |
| `gobby-clones` | Repository clones |
| `gobby-merge` | Merge resolution |
| `gobby-hub` | Hub / cross-project |
| `gobby-config` | Configuration |
| `gobby-voice` | Voice chat |
| `gobby-skills` | Skill management |
| `gobby-cron` | Scheduled jobs |
| `gobby-tests` | Test runner |

Use `list_mcp_servers()` to see all connected servers including external ones.
