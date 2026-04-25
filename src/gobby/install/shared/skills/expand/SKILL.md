---
name: expand
description: "Use when the user asks to expand a task into a concrete task tree."
category: core
triggers: expand task, break down, subtask, decompose
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby expand

Thin wrapper over the `expand-task` pipeline and the new expansion-run MCP tools.

## Supported Inputs

- `#N` or another task ref
- `path/to/plan.md` together with a target task

## Workflow

1. Resolve the target task.
2. Run the `expand-task` pipeline with:

```python
call_tool("gobby-workflows", "run_pipeline", {
    "name": "expand-task",
    "inputs": {
        "task_id": "<task_ref>",
        "plan_file": "<optional relative plan path>"
    }
})
```

3. Store the returned `execution_id` and end the turn. The daemon sends a
   durable completion notification and wake signal when the pipeline finishes.
   On wake/resume, inspect the run with:

```python
call_tool("gobby-workflows", "get_pipeline_status", {
    "execution_id": "<execution_id>"
})
```

4. Report the resulting run status and created tasks.

## Notes

- Expansion state lives on `expansion_runs`, not on the task record.
- Do not call deprecated save/execute-spec tools.
- Do not call the removed workflow completion-wait tool.
- Use `gobby-tasks-ops:get_latest_expansion_run` or `get_expansion_run` for inspection.
