---
name: agent/help-content
description: Help listing for the Gobby router command showing available skills
version: "1.0"
required_variables: [skills_list, command_prefix]
---
# Gobby Skills

Installed skills below are generated from `discover_core_skills()`. Invoke one
with `{{ command_prefix }} <skill>`:

{{ skills_list }}

**Skill discovery**: `list_skills()` / `get_skill(name="skill-name")` on
`gobby-skills`.
**Hub search**: `search_hub(query="...")` on `gobby-skills`.
**MCP tools**: call leased known tools directly. For a known unleased tool, call
`get_tool_schema` directly, then `call_tool`. Use `list_tools` only for an unknown
tool name and `list_mcp_servers` only for unknown server or registry inspection.
