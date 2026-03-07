---
name: mcp-progressive-discovery
description: MCP server instructions for progressive tool discovery
version: "1.0"
---
<gobby_system>

<tool_discovery>
Progressive discovery keeps token usage low — fetch schemas just-in-time, not upfront.

All servers follow the same discovery chain:
1. `list_mcp_servers()` — Discover available servers (required once per session)
2. `list_tools(server_name="...")` — See tool names per server (required once per server, per session)
3. `get_tool_schema(server_name, tool_name)` — Fetch parameter schema (required before first call)
4. `call_tool(server_name, tool_name, args)` — Execute the tool

The proxy validates parameters on every call_tool. If params are wrong, the error includes the full schema.
</tool_discovery>

<skills>
Discover skills with progressive discovery too:
1. `list_skills()` on `gobby-skills` — Names and descriptions
2. `get_skill(name="...")` — Full skill content (use after list_skills or search_skills)
3. `search_skills(query="...")` — Semantic search by topic (independent entry point, like list_skills)
</skills>

<code_search>
If the project has a code index, `gobby-code` provides symbol-level search and retrieval.
Key tools: `search_symbols(query)`, `get_file_outline(file_path)`, `get_symbol(symbol_id)`.
Use these instead of reading entire files — saves 90%+ tokens on large files.
</code_search>

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
</common_mistakes>

<rules>
- Create/claim a task before using Edit, Write, or NotebookEdit tools
- Pass session_id to create_task (required), claim_task (required), and close_task (optional, for tracking)
- NEVER load all tool schemas upfront — use progressive discovery
</rules>

</gobby_system>
