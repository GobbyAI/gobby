---
name: gobby
description: "Gobby help and skill discovery. Lists available skills and MCP servers."
version: "2.0.0"
category: core
triggers: help
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby — Help & Skill Discovery

You have been invoked as the `/gobby` help command.

## What to Do

1. If this is a bare `/gobby` or `/gobby help` invocation (no skill-context block below), show the user what's available:
   - Run `list_mcp_servers()` and `list_skills()` on the gobby-skills server if not already done this session
   - Show available skills with `/gobby skillname` invocation syntax
   - Show available MCP servers

2. If invoked as `/gobby skillname`, the skill content has **already been injected** into your context via hooks. Look for a `<skill-context>` block in the system context above this message and follow those instructions directly. Do **NOT** call `get_skill()` again — the content is already present.

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
/gobby commit        # Resolves to committing-changes
```

## MCP Server Discovery

For MCP tool access, use progressive discovery:
1. `list_mcp_servers()` — discover servers
2. `list_tools(server_name="...")` — discover tools
3. `get_tool_schema(server_name, tool_name)` — get parameters
4. `call_tool(server_name, tool_name, args)` — execute
