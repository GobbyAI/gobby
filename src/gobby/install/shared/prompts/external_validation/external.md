---
name: external-validation-external
description: Prompt for direct LLM validation (no tools)
version: "1.0"
variables:
  task_title:
    type: str
    required: true
    description: Task title
  criteria_section:
    type: str
    required: true
    description: Acceptance criteria or task description section
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
You are reviewing code changes for the following task.

## Task
Title: {{ task_title }}

{{ criteria_section }}{{ priority_section }}{{ symbol_section }}

## Code Changes to Validate
{{ summarized_changes }}

## Instructions
1. Review each change against the acceptance criteria
2. Check for correctness, completeness, and potential issues
3. You have no prior context about this implementation — judge only from the
   criteria and changes shown here

## Output Format
Return your assessment as a JSON object:

```json
{
  "status": "valid" | "invalid",
  "summary": "Brief assessment of the changes",
  "issues": [
    {
      "type": "acceptance_gap|test_failure|lint_error|type_error|security",
      "severity": "blocker|major|minor",
      "title": "Brief description",
      "location": "file:line (if applicable)",
      "details": "Full explanation",
      "suggested_fix": "How to resolve (if applicable)"
    }
  ]
}
```

If all criteria are met, return status "valid" with an empty issues array.
If there are problems, return status "invalid" with detailed issues.
