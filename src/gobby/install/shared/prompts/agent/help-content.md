---
name: agent/help-content
description: Help listing for /gobby command showing available skills
version: "1.0"
required_variables: [skills_list]
---
# Gobby Skills

Invoke skills directly with `/gobby skillname` syntax:

{{ skills_list }}

**MCP access**: `list_skills()` / `get_skill(name="skill-name")` on `gobby-skills`.
**Hub search**: `search_hub(query)` on `gobby-skills`.
**MCP tools**: `list_mcp_servers()` for tool discovery.
