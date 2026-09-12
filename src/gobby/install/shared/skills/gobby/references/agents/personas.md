# Switch a conversational persona

Load when the user requests an agent persona for the current conversation.
Discover candidates with `gobby-workflows:list_agent_definitions` using
`surface_filter="persona"`; inspect a candidate with `get_agent_definition`.

With no persona name, show available names and descriptions and ask the user to
choose. With a name, call `gobby-agents:apply_persona` with `agent` set to that
name. Report the successful selection and that its guidance arrives on the next
user turn. `agent="default"` restores the default persona.

Persona activation changes prompt identity, skill selection, and deferred
reinjection flags. It does not spawn a child, change provider/model/reasoning or
isolation, install active rules or tool restrictions, or create, replace, or
snapshot `agent_step_instances`. An existing spawned step instance survives.
Optional caller variables must not collide with reserved persona state; task
context is not task claiming. Do not use optional parameters to bypass a gate.

If resolution fails or the definition lacks the persona surface, report the
error and list eligible choices. Do not substitute a spawned worker. Check the
installed definition's scope and source filter when a name is unavailable.

Guide: [Usage surfaces](../../../../../../../../docs/guides/agents.md#usage-surfaces).

_Last verified: 2026-09-12_
