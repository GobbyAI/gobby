# Agents

Load when inspecting definitions, switching personas, coordinating workers, or
recovering agent runs. Load the applicable topic completely before acting; this
overview and menus do not load sibling references. Fetch unleased tool schemas
and follow every reference cursor to completion.

Use `gobby-workflows:list_agent_definitions` and `get_agent_definition` to inspect
installed definitions. `gobby-agents` owns spawning, runtime inspection, personas,
and messaging. A definition, a session, a task, and an agent run have distinct
identities; preserve the returned run and child-session identifiers.

Choose `$gobby agents references definitions`, `spawning`, `providers`, `isolation`,
`personas`, `messaging`, `checkpoints`, or `lifecycle`. Menus only display choices.
Tool schemas own arguments and defaults. CLI and HTTP maintenance is for
operators/clients; agents use MCP tools within their assigned authority.

If a launch or recovery fails, inspect its run record and exact error before
retrying. Do not duplicate active workers or clear task ownership directly.

Guide: [Agents](../../../../../../../../docs/guides/agents.md).

_Last verified: 2026-09-12_
