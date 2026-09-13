# Inspect and maintain definitions

Load before authoring definitions, changing selectors or steps, or diagnosing
why a definition is unavailable. Begin with `gobby-workflows:list_agent_definitions`
and `get_agent_definition`; inspect enabled state, project scope, sources,
surfaces, and the returned prompt blocks. Installed rows are authoritative.

Use `evaluate_agent` for definition diagnostics and `get_step_status` for the
current session instance. Create a distinct user definition
with `create_agent_definition`; use `toggle_agent_definition`,
`update_agent_rules`, `update_agent_variables`, and `update_agent_step_workflow`
for the corresponding authorized edits. Re-read after mutation. The step tool
replaces the nested workflow; `clear_step_workflow=true` clears it. `delete_agent_definition` removes
a definition through its supported lifecycle. Follow schema-specific deletion
options, including any protection of bundled content.

Each declared surface requires its nonempty prompt: `prompts.persona` for
interactive use and `prompts.agent` for spawned work. Put steps, variables, and
exit condition inside `step_workflow`. Removed top-level step and legacy prompt
fields are rejected. Use strict strings/booleans for model, reasoning, fallback,
and endpoint fields. A definition can inherit execution settings; runtime spawn
arguments select concrete isolation.

Keep reusable policy in rules and phased restrictions in steps. Step and rule
restrictions both apply. Spawned sessions snapshot steps; changing a definition
is not a live replacement of existing step instances. Persona changes never
create those instances. Bundled templates are Gobby-owned; customize through a
user copy/explicit override, not a bundled installed-row edit that sync replaces.
Verify auto-export persistence when a mutation succeeds with an export warning.

Operator/client surfaces include `gobby agents list`, `show`, `check`, and
`steps`, plus HTTP definition import/export, restore, CRUD, and selector patches.
A missing definition warrants scope/source/enablement inspection before creation;
validation failures warrant correcting the named field, not removing the gate.

Guide: [Definition storage](../../../../../../../../docs/guides/agents.md#definition-storage)
and [inline steps](../../../../../../../../docs/guides/agents.md#inline-step-workflows).

_Last verified: 2026-09-12_
