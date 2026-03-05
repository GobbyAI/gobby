---
name: mcp-progressive-discovery
description: MCP server instructions for progressive tool discovery
version: "1.0"
---
<gobby_system>

<tool_discovery>
Progressive discovery keeps token usage low — fetch schemas just-in-time, not upfront.

Internal servers (gobby-tasks, gobby-memory, etc.) are pre-discovered at session start.
For these, skip straight to step 3 or 4:
3. `get_tool_schema(server_name, tool_name)` — Fetch parameter schema (recommended before first call)
4. `call_tool(server_name, tool_name, args)` — Execute the tool

External MCP servers require full discovery:
1. `list_mcp_servers()` — Discover available servers
2. `list_tools(server_name="...")` — See tool names per server
3-4. Same as above

The proxy validates parameters on every call_tool. If params are wrong, the error includes the full schema.
</tool_discovery>

<skills>
Discover skills with progressive discovery too:
1. `list_skills()` on `gobby-skills` — Names and descriptions
2. `get_skill(name="...")` — Full skill content (use after list_skills or search_skills)
3. `search_skills(query="...")` — Semantic search by topic (independent entry point, like list_skills)
</skills>

<caching>
Schema fetches are cached per session. Once you call `get_tool_schema(server_name, tool_name)`,
you can `call_tool` repeatedly WITHOUT re-fetching. Only fetch on first use.
</caching>

<common_mistakes>
WRONG — Loading all schemas upfront (wastes 30-40K tokens):
  for server in servers: get_tool_schema(server, tool) for each tool

RIGHT — Just-in-time discovery:
  get_tool_schema("gobby-tasks", "create_task")  # Learn required params
  call_tool("gobby-tasks", "create_task", {"title": "Fix bug", "session_id": "#123"})

ALSO OK — Direct call when you know the params:
  call_tool("gobby-tasks", "create_task", {"title": "Fix bug", "session_id": "#123"})
  # If params are wrong, error includes full schema for self-correction
</common_mistakes>

<rules>
- Create/claim a task before using Edit, Write, or NotebookEdit tools
- Pass session_id to create_task (required), claim_task (required), and close_task (optional, for tracking)
- NEVER load all tool schemas upfront — use progressive discovery
</rules>

</gobby_system>
