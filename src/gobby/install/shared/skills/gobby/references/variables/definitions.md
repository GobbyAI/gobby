# Variable definitions

Load when discovering, creating, updating, exporting or retiring a default.
Fetch `gobby-workflows` schemas for `list_variables`, `get_variable_definition`,
`create_variable`, `update_variable`, `delete_variable` and `export_variable`.

MCP definition creation, updating and deletion require
`manage_workflow_definitions` or the operator's terminal. Other seats can read
and export definitions and ask an authorized session to mutate them. Session
`set_variable` is separate and requires `write_session_variables`, or the narrower
`write_own_step_phase` for the caller's own step `phase` alone.

Read the existing row first; inspect scope, enabled state, source and tags.
Creation rejects name collisions and creates a global user definition. The MCP
create/update `value` schema is currently string-only, without runtime coercion;
use typed user YAML or an authorized operator API for non-string defaults.
`project_path` controls file export, not DB scope. Update null means unchanged.

Export produces named YAML, not a metadata-complete backup. Check DB and file
results separately: export can be skipped or fail after successful persistence.
Soft-delete removes a definition, not any session override. Bundled rows can return
on sync; do not use force deletion or permissive mutators to bypass Gobby ownership.
For missing names or collisions, inspect scope and use a user-owned definition.
See [definition tools](../../../../../../../../docs/guides/variables.md#mcp-tools) and [overrides](../../../../../../../../docs/guides/variables.md#defaults-and-overrides).

_Version: 1.1.0_
_Last verified: 2026-11-03_
