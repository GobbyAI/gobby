---
name: validation-validate
description: Base prompt for validating task completion against criteria
version: "1.0"
variables:
  title:
    type: str
    required: true
    description: Task title
  category_section:
    type: str
    default: ""
    description: Optional category/test strategy section
  criteria_text:
    type: str
    required: true
    description: Validation criteria or task description
  changes_section:
    type: str
    required: true
    description: Summary of changes made (files, diffs, etc.)
  file_context:
    type: str
    default: ""
    description: Optional file content context
---
Validate if the following changes satisfy the requirements.

You are validating completion, not explaining around gaps. Return `invalid` if any
acceptance criterion is unmet, any required command/lint/type/test gate is missing or
failing, or required evidence is absent. Do not return `valid` while describing a
failed criterion, non-clean mypy/ruff/test result, missing verification, or errors
that prevented a required gate from passing.

If evidence is explicitly unavailable because the diff or file context exceeded
the validation prompt budget, return `pending` with feedback naming the unavailable
evidence. Do not treat `[file diff truncated]`, `[changes section truncated]`, or
`[file context truncated]` as proof that a file is missing or incomplete; use the
changed-file manifest and available file context.

Task: {{ title }}
{{ category_section }}{{ criteria_text }}

{{ changes_section }}
{% if file_context %}
File Context:
{{ file_context }}
{% endif %}
IMPORTANT: Return ONLY a JSON object, nothing else. No explanation, no preamble.
Format: {"status": "valid", "feedback": "..."}, {"status": "invalid", "feedback": "..."},
or {"status": "pending", "feedback": "..."}
