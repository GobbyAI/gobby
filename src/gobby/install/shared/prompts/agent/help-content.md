---
name: agent/help-content
description: Immediate project-aware Gobby help
version: "3.0"
required_variables: [skills_list, capabilities_list, command_prefix]
---
Display this menu immediately and finish. Make zero tool calls. Defer pending
housekeeping and bootstrap instructions to the next work request.

# Gobby

Capabilities:

{{ capabilities_list }}

Installed skills:

{{ skills_list }}

Use `{{ command_prefix }} <capability> references` for topics.
Choose a command to continue; this menu executes no operations.
