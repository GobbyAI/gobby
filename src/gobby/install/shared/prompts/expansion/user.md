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
  plan_file:
    type: str
    default: ""
  single_phase_mode:
    type: bool
    default: false
  phase_title:
    type: str
    default: ""
  phase_number:
    type: int
    default: 0
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

## Plan File
{{ plan_file if plan_file else "No plan file provided." }}

## Requirements
{% if single_phase_mode %}
**Single-phase compile.** This run is scoped to Phase {{ phase_number }}: "{{ phase_title }}".
Return exactly one phase entry in `phases[]` titled "{{ phase_title }}". Do not invent
sibling phases — the merger combines this output with independent runs for the other
phases of the plan. All `tasks[]` entries must reference this phase's `id`.
{% endif %}
- Produce a phase-aware compiled spec.
- Prefer multiple phases only when the work is genuinely phased.
- Make dependencies explicit with stable task IDs.
- Include explicit `test_intent` for every phase.
- Use `affected_files` and `execution_group` only when the repo context supports them.
- Keep the task graph complete but minimal.
