# Workflows Source Reference

This directory implements Gobby's rule engine, pipeline executor, and workflow infrastructure.

## Key Classes

| Class | File | Purpose |
|-------|------|---------|
| `RuleEngine` | `rule_engine.py` | Evaluates rules against hook events. Priority-sorted, first-block-wins. |
| `PipelineExecutor` | `pipeline_executor.py` | Executes pipeline steps sequentially with typed data flow. |
| `SafeExpressionEvaluator` | `safe_evaluator.py` | AST-based condition evaluator (no `eval()`). Also defines `LazyBool`. |
| `StateManager` | `state_manager.py` | Session variable persistence (read/write to `session_variables` table). |
| `WorkflowLoader` | `loader.py` | Loads YAML definitions (rules, agents, pipelines, variables). |

## File Index

### Rule Engine
- `rule_engine.py` — Core rule evaluation: event matching, condition eval, effect dispatch, auto-management (stop counting, consecutive block tracking)
- `safe_evaluator.py` — `SafeExpressionEvaluator` (AST-based), `LazyBool` (deferred booleans)
- `condition_helpers.py` — Built-in condition functions: `task_tree_complete`, `mcp_called`, `has_stop_signal`, progressive discovery helpers
- `definitions.py` — All definition models: `RuleDefinitionBody`, `RuleEffect`, `RuleEvent`, `AgentDefinitionBody`, `WorkflowStep`, `PipelineDefinition`, `PipelineStep`, `VariableDefinitionBody`

### Pipeline Executor
- `pipeline_executor.py` — Pipeline execution engine, wait step handling, nested pipeline invocation
- `pipeline_state.py` — Pipeline state management and persistence
- `pipeline_webhooks.py` — Webhook notifications for pipeline events
- `pipeline/` — Step handlers subdirectory:
  - `pipeline/handlers.py` — Step type handlers (exec, prompt, mcp, activate_workflow)
  - `pipeline/renderer.py` — `StepRenderer`: Jinja2 template rendering, condition evaluation, type coercion
  - `pipeline/gatekeeper.py` — `ApprovalManager`: approval gate token management

### Definitions & Loading
- `definitions.py` — Pydantic models for all workflow types (~560 lines, central schema file)
- `loader.py` — Main YAML loader entry point
- `loader_sync.py` — Syncs bundled templates to database on daemon start
- `loader_validation.py` — Validates YAML structure before import
- `loader_discovery.py` — Discovers YAML files in template directories
- `loader_cache.py` — Caches loaded definitions

### Agent & Workflow Resolution
- `agent_resolver.py` — Resolves agent definitions from database
- `selectors.py` — Selector engine: matches rules/skills/variables by name, tag, group, source
- `state_manager.py` — Session variable CRUD (persisted in `session_variables` table)

### Templates & Rendering
- `templates.py` — Jinja2 template rendering for rule effects
- `workflow_templates.py` — Template generation utilities

### Actions & Hooks
- `hooks.py` — Hook integration: bridges hook events to the rule engine
- `task_actions.py` — Task-related actions triggered by rules
- `task_claim_state.py` — Tracks task claim state for session variables
- `summary_actions.py` — Session summary generation for context handoff
- `observers.py` — Observer pattern for workflow events

### Other
- `sync.py` — Syncs workflow definitions between YAML templates and database
- `enforcement/` — Enforcement helpers (progressive discovery blocking logic)
- `lobster_compat.py` — Lobster format importer
- `git_utils.py` — Git utilities for rule conditions (dirty file detection)
- `constants.py` — Shared constants
- `dry_run.py` — Dry-run evaluation support
- `webhook.py`, `webhook_executor.py` — Webhook dispatching

## Database Tables

| Table | Purpose |
|-------|---------|
| `workflow_definitions` | All definitions: rules, agents, pipelines, variables |
| `session_variables` | Per-session variable state |
| `rule_overrides` | Per-session rule enable/disable overrides |
| `pipeline_executions` | Pipeline execution records |
| `step_executions` | Individual step execution records |

## Entry Points

- **Rule evaluation**: `RuleEngine.evaluate()` — called by `hooks.py` on every hook event
- **Pipeline execution**: `PipelineExecutor.execute()` — called by MCP tools and CLI
- **Variable access**: `StateManager.get_variables()` / `set_variable()`

## Guides

- [Rules](../../docs/guides/rules.md) — Rule YAML format, events, effects, conditions
- [Pipelines](../../docs/guides/pipelines.md) — Pipeline schema, step types, data flow
- [Variables](../../docs/guides/variables.md) — Session variables, initialization, mutation
- [Workflows Overview](../../docs/guides/workflows-overview.md) — Mental model and composition
