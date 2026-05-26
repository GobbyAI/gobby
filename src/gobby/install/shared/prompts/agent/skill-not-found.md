---
name: agent/skill-not-found
description: Message when a requested skill is not found
version: "1.0"
required_variables: [skill_name]
optional_variables: [close_matches, command_prefix]
---
Skill '{{ skill_name }}' not found.
{% set skill_command = command_prefix | default('gobby skills', true) %}
{% if close_matches %}

Did you mean:
{% for match in close_matches %}
  - `{{ skill_command }} {{ match }}`
{% endfor %}
{% endif %}

Run `{{ skill_command }}` or `{{ skill_command }} help` to see all available skills.
