---
name: gobby
description: "Gobby help and skill discovery. Lists available skills and MCP servers."
version: "2.0.0"
category: core
triggers: help
---

# /gobby — Help & Skill Discovery

You have been invoked as the `/gobby` help command.

## What to Do

1. If this is a bare `/gobby` or `/gobby help` invocation, show the user what's available:
   - Run `list_mcp_servers()` and `list_skills()` on the gobby-skills server if not already done this session
   - Show available skills with `/gobby skillname` invocation syntax
   - Show available MCP servers

2. If invoked as `/gobby skillname`, hooks provide a directive instead of the skill body. Call get_skill(name="<skill>") on gobby-skills, then continue with the user's arguments.

## Skill Invocation

Users invoke skills with `/gobby skillname` syntax:

```text
/gobby tasks         # Task management
/gobby expand        # Expand task into subtasks
/gobby plan          # Specification planning
/gobby memory        # Persistent memory
/gobby sessions      # Session management
/gobby worktrees     # Git worktree management
/gobby merge         # AI merge conflict resolution
/gobby agents        # Agent spawning
/gobby doctor        # Systems diagnostics
/gobby commit        # Resolves to source-control
```

## MCP Server Discovery

For MCP tool access, use progressive discovery:
1. `list_mcp_servers()` — discover servers
2. `list_tools(server_name="...")` — discover tools
3. `get_tool_schema(server_name, tool_name)` — get parameters
4. `call_tool(server_name, tool_name, args)` — execute
