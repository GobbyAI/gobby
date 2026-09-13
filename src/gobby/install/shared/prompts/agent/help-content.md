---
name: agent/help-content
description: Catalog capabilities and dynamically discovered installed standalone skills
version: "2.0"
required_variables: [skills_list, capabilities_list, command_prefix]
---
# Gobby

Capabilities:

{{ capabilities_list }}

Use `{{ command_prefix }} <capability> references` for topic descriptions and
exact invocation examples. Menus list choices only; do not execute their operations.

Installed skills below are generated from `discover_core_skills()`. Invoke one
with `{{ command_prefix }} <skill>`, or explicitly with
`{{ command_prefix }} skill <skill>` when its name collides with a capability:

{{ skills_list }}

**Skill discovery**: `list_skills()` / `get_skill(name="skill-name")` on
`gobby-skills`.
**Hub search**: `search_hub(query="...")` on `gobby-skills`.
**MCP tools**: call leased known tools directly. For a known unleased tool, call
`get_tool_schema` directly, then `call_tool`. Use `list_tools` only for an unknown
tool name and `list_mcp_servers` only for unknown server or registry inspection.
