---
name: expansion-user
description: User prompt template for compiled task expansion
version: "2.1"
variables:
  task_id:
    type: str
    required: true
  title:
    type: str
    required: true
  description:
    type: str
    default: ""
  context_str:
    type: str
    default: ""
  research_str:
    type: str
    default: ""
  enabled_stages:
    type: list
    default: []
---
Compile this task into a deterministic expansion spec.

## Parent Task
- ID: {{ task_id }}
- Title: {{ title }}
- Description:
{{ description }}

## Repository Context
{{ context_str }}

## Additional Research
{{ research_str }}

## Requirements
- Produce a phase-aware compiled spec.
- Prefer multiple phases only when the work is genuinely phased.
- Make dependencies explicit with stable task IDs.
- Include explicit `test_intent` for every phase.
- Use `affected_files` and `execution_group` only when the repo context supports them.
- Keep the task graph complete but minimal.
- Agent Selection runs after leaf generation and before returning the spec. Use the
  `expansion-agent-selection` skill heuristics and available agent definitions to emit
  `assigned_agent` for every automated `code`, `config`, `docs`, `refactor`, or `test` leaf.
- Emit `additional_skills` as an array, usually empty, when a leaf needs skills beyond
  the assigned agent's baseline.
- Do not emit `planning` or `research` leaves; approved-plan expansion must be development-forward.
- Enabled parent stages: {{ enabled_stages | join(", ") if enabled_stages else "none" }}
