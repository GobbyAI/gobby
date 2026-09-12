# Tasks

Load for discovering or organizing task work. Load the relevant operation topic
before creating, changing, reviewing, or closing tasks. This overview and topic
menus do not satisfy operation-specific instruction requirements.

Use `gobby-tasks` through the MCP proxy for agent task lifecycle work. Fetch a
known unleased tool's schema directly before its first call; discover unknown
names with `list_tools`. Pass the caller's session on the outer `call_tool`.
Tool schemas own parameters and defaults. The task CLI and HTTP lifecycle routes
are operator interfaces, not substitutes for agent ownership and close gates.

Start with `list_ready_tasks`, `suggest_next_task`, or `search_tasks`; use
`list_tasks` for parent, label, project, closed-state, or current-stage filters.
Read the selected task with `get_task(brief=false)`, including dependencies and
acceptance criteria. A listing is bounded by its schema's limit; do not assume
the first result set is a complete project inventory.

Task state projects ownership, ordered stages, closure, escalation, and blockers.
It is not an editable status string. Claim before editing, track implementation
substeps in the provider's native tracker, preserve other sessions' files, then
validate, commit, and close through the appropriate lifecycle tool.

Task refs include project-local `#N`, dotted task paths, and UUIDs. Resolve the
project first when context is ambiguous. Use `get_session_tasks`,
`get_task_sessions`, and `link_task_to_session` to trace work and evidence;
linking a session does not claim the task.

Choose `$gobby tasks references creation`, `dependencies`, `implementation`,
`closing`, `reviews`, `artifacts`, `backups`, or `live-work` for the next action.
For plan expansion use `$gobby plan`; for dispatch use `$gobby build`.
Unknown names should return choices without executing operations.

Recovery: inspect the returned blocker and current task before changing state.
Do not force a claim, overwrite a foreign path, or fabricate completion evidence
to bypass a gate.

Guide: [Task Management](../../../../../../../../docs/guides/tasks.md#agent-workflow).

_Last verified: 2026-09-12_
