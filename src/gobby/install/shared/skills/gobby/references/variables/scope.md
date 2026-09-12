# Variable scope

Load before choosing project/global defaults, a target session, or step values.
Top-level variable tools are session-only and require explicit `session_id`.
Definitions are a different API: MCP lists can contain multiple project scopes,
while name-based management is not a project-selection API.

For user YAML, loader roots supply global/project scope. `create_variable` MCP
creates a global row; `project_path` chooses export destination only. Operator
HTTP definition creation supports `project_id`. Inspect returned scope.

Session HTTP get/set supports `scope=session|step`, default session. Step scope
requires an existing agent-step instance and reads/writes only that instance map.
`gobby-workflows:get_step_status` is a shared diagnostic, not instance creation.
A missing instance or session mismatch is an error to investigate. Agent tokens
remain bound to their session; operator definition routes need operator authority.
See [HTTP and scope](../../../../../../../../docs/guides/variables.md#http-and-scope). Never create a new runtime instance or
retarget another session merely to make a variable request succeed.
