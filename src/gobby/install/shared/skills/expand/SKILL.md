---
name: expand
description: "Use when the user asks to expand a task into a concrete task tree."
version: "1.0.0"
category: core
triggers: expand task, break down, subtask, decompose
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby expand

Thin wrapper over the `expand-task` pipeline and the new expansion-run MCP tools.

## Plan-Coverage Contract

Expansion consumes typed plans authored under the Plan-Coverage Contract in
`src/gobby/install/shared/skills/plan-draft/SKILL.md`; the single-page
reference is `docs/contracts/plan-coverage.md`.

Expansion-side obligations:

- Created leaves MUST emit structured
  `covers:<plan-id>:<section-id>:<item-id>` labels for the acceptance items
  they implement.
- `expansion-qa` is the mechanical gate that compares the expanded task tree
  against the compiled contract and records any missing or invalid coverage.
  The pipeline runs it automatically for every contract-plan run
  (`coverage_check` step; its result is the pipeline's `coverage` output).
- Apply creates one task per `kind: deferred` section whose `task_ref` is a
  placeholder; the run checkpoint `deferral_task_map` maps section id to the
  created task id. The task is labeled `needs-planning` (a hold label that
  automated dispatch skips) and is blocked by the leaf of every section the
  deferred heading's `(depends: ...)` names.
- Free-form `plan-ref:` labels are not honored.

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

4. Report the resulting run status, created tasks, and the `coverage` output.
   A contract-plan run whose coverage did not pass is not done: fix the
   missing or invalid rows and rerun `gobby-tasks-ops:run_expansion_qa_coverage`.
5. For a contract plan with placeholder deferral refs, read
   `checkpoints.deferral_task_map` from `get_expansion_run`, write each created
   `#N` over the matching `task_ref` in the plan file, re-run
   `gobby plans validate <plan>`, call `gobby-tasks-ops:update_plan_hash` for the
   root task, and commit the plan.

## Notes

- Expansion state lives on `expansion_runs`, not on the task record.
- Do not call deprecated save/execute-spec tools.
- Do not call the removed workflow completion-wait tool.
- Use `gobby-tasks-ops:get_latest_expansion_run` or `get_expansion_run` for inspection.
