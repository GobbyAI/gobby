# Workflow Definition Storage Refactor

## Summary

Replace the overloaded `workflow_definitions` table and `workflow_type` API
surface with definition-centric storage for exactly four public definition
kinds: `rule`, `agent`, `variable`, and `pipeline`.

This is a clean schema break. Existing databases using `workflow_definitions`
must be reset or recreated. Standalone workflow/step definition storage is
removed; agent step workflows remain inline in agent definitions and are
materialized only at runtime through agent/session state.

## Implementation Tasks

1. Schema and storage foundation
   - Replace `workflow_definitions` in the baseline schema with
     `definition_registry` plus typed body tables: `rule_definitions`,
     `agent_definitions`, `variable_definitions`, and `pipeline_definitions`.
   - Add a schema guard during PostgreSQL startup that detects old
     `workflow_definitions` databases and fails with explicit reset/reinit
     instructions.
   - Replace `LocalWorkflowDefinitionManager` with typed managers:
     `RuleDefinitionManager`, `AgentDefinitionManager`,
     `VariableDefinitionManager`, and `PipelineDefinitionManager`.
   - Enforce kind-aware lookup everywhere. Same names across different kinds are
     valid; same-name same-kind ambiguity is an error.

2. Runtime loader migration
   - Rule engine loads rules through `RuleDefinitionManager`.
   - Session variable defaults load through `VariableDefinitionManager`.
   - Agent spawn, dry-run, dispatch, stage registry, fallback resolution, and
     persona application load through `AgentDefinitionManager`.
   - Pipeline loader, executor, MCP exposure, and bundled sync load through
     `PipelineDefinitionManager`.
   - Remove DB-backed standalone workflow/step loader paths. Inline agent steps
     are read from agent definitions and bound to `workflow_instances` as
     runtime state only.

3. Public API cleanup
   - HTTP, MCP, CLI, YAML import/export, command output, route labels, docs, and
     web UI expose `definition_kind`.
   - Remove public `workflow_type` fields from final-state responses.
   - Remove generic workflow-definition CRUD tools/routes. Keep domain-specific
     rule, agent, variable, and pipeline APIs.
   - Reject public creation/loading/listing of standalone `workflow` or `step`
     definitions.

4. Bundled sync and templates
   - Update bundled rule, variable, agent, and pipeline sync to write through
     typed managers.
   - Preserve bundled `gobby` tags, enabled flags, soft-delete behavior, and
     user/custom overwrite protection.
   - Update drift detection and auto-export helpers to work with typed
     definition rows.

5. Task decomposition
   - Treat the broad refactor as an anchor/plan, not one implementation task.
   - Split implementation into focused tasks for schema/storage, runtime
     loaders, public API cleanup, bundled sync/templates, and tests/docs.

## Test Plan

- Storage tests: typed CRUD, restore/delete, project/global precedence,
  same-name cross-kind safety, same-kind ambiguity errors, invalid body
  validation.
- Runtime tests: rule evaluation, variable defaults, agent resolution/spawn
  dry-run, stage registry default agent lookup, persona application, pipeline
  load/execution.
- Public API tests: HTTP/MCP/CLI/UI use `definition_kind`; `workflow_type` is
  absent from final response payloads.
- Legacy blocker tests: startup fails cleanly when `workflow_definitions` exists;
  standalone workflow/step creation is rejected.
- Audit tests: production SQL must not reference removed `workflow_definitions`;
  public response fixtures must not expose `workflow_type`.

## Validation

Run focused validation per implementation task, not the full suite:

- `GOBBY_TEST_PROTECT=1 uv run pytest tests/storage tests/workflows/test_rule_engine.py tests/workflows/test_session_defaults.py -v`
- `GOBBY_TEST_PROTECT=1 uv run pytest tests/mcp_proxy/tools/workflows tests/servers/routes -v`
- Add narrower command-specific tests for CLI/UI changes as those tasks land.

## Assumptions

- Clean break is acceptable; existing local/PostgreSQL hub data can be reset.
- Public supported definition kinds are exactly `rule`, `agent`, `variable`, and
  `pipeline`.
- Pipeline YAML remains under workflow install paths for now, but storage/API
  language becomes definition-centric.
- Agent step workflows stay inline in agent definitions and are not stored as
  standalone definitions.
