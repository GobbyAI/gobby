# Tasks Source Reference

This directory implements task expansion, validation, and related utilities.

## Key Files

| File | Purpose |
|------|---------|
| `validation.py` | `TaskValidator`: validates task completion using LLM-based analysis of commits, diffs, and criteria |
| `enhanced_validator.py` | Enhanced validation with multi-pass analysis and structured feedback |
| `external_validator.py` | External validation support (e.g., running test commands) |
| `validation_models.py` | Pydantic models for validation results, issues, feedback |
| `validation_history.py` | Tracks validation iterations, recurring issues, fix attempts |
| `build_verification.py` | Builds verification context (commands, file state) for validators |
| `escalation.py` | Task escalation logic (when tasks need human intervention) |
| `issue_extraction.py` | Extracts structured issues from validation feedback |
| `tree_builder.py` | Builds task dependency trees for display and analysis |
| `commits.py` | Links git commits to tasks, diff generation |

## Prompts

The `prompts/` directory contains LLM prompts used during expansion:

| File | Purpose |
|------|---------|
| `prompts/expand-task.md` | Main expansion prompt: spec format, rules, validation criteria guidelines |
| `prompts/expand-task-tdd.md` | TDD mode instructions: sandwich pattern, category rules |

## Expansion Flow

Task expansion is handled by the `expand-task` pipeline (not code in this directory). The MCP tools for expansion live in `src/gobby/mcp_proxy/tools/tasks/_expansion.py`:

- `save_expansion_spec` — Save JSON spec to task (called by expander agent)
- `validate_expansion_spec` — Validate spec structure and dependencies
- `execute_expansion` — Atomically create subtasks from spec
- `get_expansion_spec` — Check for pending spec (resume support)

## Validation Flow

```
validate_task() called
  → Build verification context (commands, file state)
  → Gather commit diffs linked to task
  → Send to LLM with validation_criteria
  → Parse structured feedback (pass/fail, issues)
  → Store in validation_history
  → Return result
```

## Guides

- [Task Expansion](../../docs/guides/task-expansion.md) — How expansion works end-to-end
- [TDD Enforcement](../../docs/guides/tdd-enforcement.md) — TDD sandwich pattern
