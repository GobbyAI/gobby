---
name: run-check
description: "Run project verification checks (tests, lint, typecheck) with token-efficient output via gobby-tests."
category: testing
triggers: run tests, run lint, run check, test, lint, typecheck, verify, run_check
metadata:
  gobby:
    audience: all
    format_overrides:
      autonomous: full
---

# /gobby:run-check - Run Verification Check

Run project test/lint/typecheck commands through gobby-tests for
token-efficient output. Resolves commands from project.json verification config.

## Usage

/gobby:run-check <category>
/gobby:run-check <category> <paths>

Categories come from your .gobby/project.json verification section
(e.g., unit_tests, lint, type_check, ts_check, format).

## Tool Schema Reminder

First time calling a tool this session? Use get_tool_schema(server_name, tool_name)
before call_tool to get correct parameters.

## Action

1. List available categories from project.json verification section
2. Call run_check with the requested category
3. Report the summary to the user
4. If failures: offer to show raw output via get_run_result

### Run a check

call_tool(
    server_name="gobby-tests",
    tool_name="run_check",
    arguments={
        "category": "<category from user input>",
        "paths": "<optional path override>",
        "extra_args": "<optional extra arguments>"
    }
)

### Get raw output (if needed)

call_tool(
    server_name="gobby-tests",
    tool_name="get_run_result",
    arguments={
        "run_id": "<run_id from run_check result>",
        "include_output": true,
        "output_limit": 50
    }
)

## Error Handling

- Category not found: list available categories from project.json
- No verification config: suggest running gobby init or adding manually
- Timeout: report timeout, suggest get_run_status to check later
