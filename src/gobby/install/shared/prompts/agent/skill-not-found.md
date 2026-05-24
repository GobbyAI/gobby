---
name: agent/skill-not-found
description: Message when a requested skill is not found
version: "1.0"
required_variables: [skill_name]
optional_variables: [close_matches, command_prefix]
---
Skill '{{ skill_name }}' not found.
{% if close_matches %}

Did you mean:
{% for match in close_matches %}
  - `{{ command_prefix }} {{ match }}`
{% endfor %}
{% endif %}

Run `{{ command_prefix }}` or `{{ command_prefix }} help` to see all available skills.
