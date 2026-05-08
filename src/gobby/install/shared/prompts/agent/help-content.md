---
name: agent/help-content
description: Help listing for /gobby command showing available skills
version: "1.0"
required_variables: [skills_list]
---
# Gobby Skills

Installed skills below are generated from `discover_core_skills()`. Invoke one
with `/gobby <skill>`:

{{ skills_list }}

**Skill discovery**: `list_skills()` / `get_skill(name="skill-name")` on
`gobby-skills`.
**Hub search**: `search_hub(query="...")` on `gobby-skills`.
**MCP tools**: use progressive discovery: `list_mcp_servers()`,
`list_tools(server_name="...")`, `get_tool_schema(server_name="...", tool_name="...")`,
then `call_tool(server_name="...", tool_name="...", arguments={...})`.
