# Session and agent relationships

Load when tracing parentage, task ownership, continuation identity, or an agent
run. Begin with `gobby-sessions:get_session` and the applicable agent query schema.
Use the actual session UUID and run ID; a display title is not identity.

Distinguish an agent's parent session from a clear predecessor. Compact stays on
the same row. A staged clear binds a direct successor and records delivery;
manual clear is independent. Hookless registration can supply parent metadata,
but metadata alone neither stages a handoff nor transfers claims.

Use session task links and `get_session_commits` to navigate work, then inspect
the task and commit itself. Session status and `agent_depth` are not task-close
evidence. Do not repair ownership through direct database writes or substitute
session identities.

Cooperative spawned workers call `gobby-agents:end_agent_run` with structured
current state and next steps, including on a task-blocker handoff. Their parent
or a bound clear successor reads the delivered final content through
`gobby-sessions:get_handoff(agent_run_id=...)`; reads are repeatable and access
outside that relationship is denied. Forced kills or crashes may have no authored
content. Inspect the run's terminal result rather than inventing a handoff.

`mark_loop_complete` only sets the session variable `stop_reason="completed"`.
It does not close tasks or finish an agent run; inspect the installed workflow
before assuming it controls chaining. Keep existing close and lifecycle gates.

HTTP clients can set, inspect, and clear session stop signals and close/delete
ACP provider conversations. A stop signal is distinct from a task closure or a
delivered agent-end handoff. Use these only within the intended client lifecycle;
agents use their run lifecycle tools and inspect the resulting run state.

Guide: [Handoff boundaries](../../../../../../../../docs/guides/sessions.md#handoff-boundaries).

_Last verified: 2026-09-12_
