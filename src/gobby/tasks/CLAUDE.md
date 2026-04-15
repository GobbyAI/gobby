# Tasks Source Reference

This directory implements task expansion, validation, and related utilities.

## Key Files

| File | Purpose |
|------|---------|
| `validation.py` | `TaskValidator`: validates task completion using LLM-based analysis of commits, diffs, and criteria |
| `validation_models.py` | Pydantic models for validation results, issues, feedback |
| `validation_history.py` | Tracks validation iterations, recurring issues, fix attempts |
| `tree_builder.py` | Builds task dependency trees for display and analysis |
| `commits.py` | Links git commits to tasks, diff generation |

## Prompts

The `prompts/` directory contains LLM prompts used during expansion:

| File | Purpose |
|------|---------|
| `prompts/expand-task.md` | Main expansion prompt: spec format, rules, validation criteria guidelines |
| `prompts/expand-task-tdd.md` | TDD mode instructions: sandwich pattern, category rules |

## Expansion Flow

Task expansion is handled by the `expand-task` pipeline and the shared
`ExpansionService` in `src/gobby/tasks/expansion_service.py`.

Operational expansion state lives in `expansion_runs`, not on task records.
The MCP tools for expansion live in `src/gobby/mcp_proxy/tools/tasks/_expansion.py`:

- `start_expansion_run` — Create a run and start background compile/apply
- `get_expansion_run` — Inspect current run status and stored artifacts
- `get_latest_expansion_run` — Find the most recent run for a task
- `resume_expansion_run` — Resume a failed or interrupted run
- `cancel_expansion_run` — Cancel an active run
- `validate_expansion_run` — Validate compiled and applied run structure
- `save_expansion_qa_result` — Store QA findings on a run
- `check_expansion_qa_result` — Read QA findings from a run

## Validation Flow

```text
validate_task() called
  → Gather commit diffs linked to task
  → Send to LLM with validation_criteria
  → Store in validation_history
  → Return result
```

## Guides

- [Task Expansion](../../docs/guides/task-expansion.md) — How expansion works end-to-end
- [TDD Enforcement](../../docs/guides/tdd-enforcement.md) — TDD sandwich pattern
