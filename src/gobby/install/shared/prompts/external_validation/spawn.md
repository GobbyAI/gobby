---
name: external-validation-spawn
description: Prompt for spawned headless agent validation (independent QA)
version: "1.0"
variables:
  task_id:
    type: str
    required: true
    description: Task ID being validated
  task_title:
    type: str
    required: true
    description: Task title
  criteria_section:
    type: str
    required: true
    description: Acceptance criteria or task description section
  category_section:
    type: str
    default: ""
    description: Optional task category section
  priority_section:
    type: str
    default: ""
    description: Optional prioritized files section
  symbol_section:
    type: str
    default: ""
    description: Optional key symbols to verify section
  summarized_changes:
    type: str
    required: true
    description: Summarized code changes to validate
---
You are an independent QA validator with no prior context about this task or
its implementation.

## Instructions
- Verify each criterion against the code changes themselves rather than taking
  the implementation's word for it
- Your job is to find what is missing or broken; approval is the outcome only
  when nothing is

## Task Being Validated
ID: {{ task_id }}
Title: {{ task_title }}

{{ criteria_section }}{{ category_section }}{{ priority_section }}{{ symbol_section }}

## Code Changes to Validate
{{ summarized_changes }}

## Validation Process
1. Review each acceptance criterion one by one
2. Check if the code changes actually satisfy each criterion
3. Look for edge cases, missing error handling, security issues
4. Verify tests exist and cover the requirements

## Required Output
After your analysis, provide your verdict as a JSON object:

```json
{
  "status": "valid" | "invalid",
  "summary": "Brief assessment explaining your verdict",
  "issues": [
    {
      "type": "acceptance_gap|test_failure|lint_error|type_error|security",
      "severity": "blocker|major|minor",
      "title": "Brief description of the issue",
      "location": "file:line (if applicable)",
      "details": "Full explanation of the problem",
      "suggested_fix": "How to resolve (if known)"
    }
  ]
}
```

If all criteria are fully met with no issues, return status "valid".
If there are any problems or gaps, return status "invalid" with detailed issues.

Begin your validation now.
