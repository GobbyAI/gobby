# Pipelines and scheduling

Load when choosing or operating deterministic automation. Use pipelines for
ordered steps, cron for timing, and the build capability for task lifecycle dispatch.
A scheduled pipeline composes those responsibilities without moving task ownership.

Discover definitions with `gobby-workflows:list_pipelines`, inspect the intended
one with `get_pipeline`, and export full YAML when reviewing step details. Discover
schedules with `gobby-cron:list_cron_jobs(project_id=...)`. Installed rows determine
availability; template files and a discovery menu do not prove enabled state.

Fetch each known tool's schema before first use in the current context. Load the
operation topic, inspect its target and effects, and perform only authorized work.
MCP run returns an execution ID and subscribes the caller to completion: retain
it and yield. Menus, validation and history inspection do not authorize execution.

| Topic | Load when |
| --- | --- |
| `authoring` | Creating, editing, exporting or retiring definitions |
| `validation` | Checking YAML, expressions and safe examples |
| `execution` | Starting pipelines or composing steps |
| `approvals` | Inspecting or resolving approval gates |
| `recovery` | Handling failure, cancellation or restart |
| `history` | Finding runs, outputs or deleting retained history |
| `scheduling` | Creating, changing or diagnosing cron jobs |

For missing definitions, check scope and installation. For active work, inspect
its execution ID before retrying. Use the capability owning a child operation
(agents, plan, source-control or code-index Ask) for its independent lifecycle.

Verified guide: [pipelines.md](../../../../../../../../docs/guides/pipelines.md).

_Last verified: 2026-09-12_
