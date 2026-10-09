# Switch a conversational persona

Load when the user requests an agent persona for the current conversation.
Discover candidates with `gobby-workflows:list_agent_definitions` using
`surface_filter="persona"`; inspect a candidate with `get_agent_definition`.

With no persona name, show available names and descriptions and ask the user to
choose. With a name, call `gobby-agents:apply_persona` with `agent` set to that
name. Report the successful selection and that its guidance arrives on the next
user turn. `agent="default"` restores the default persona.

On a session with an active non-default definition, `agent="default"` removes
the persona overlay and returns to that definition's own prompt and skill set.
Its rules, tool blocks, and step workflow stay in force. The persona's prompt,
skill set, and skill format survive compaction and resume.

Persona activation changes prompt identity, skill selection, and deferred
reinjection flags. It does not spawn a child, change provider/model/reasoning or
isolation, install active rules or tool restrictions, or create, replace, or
snapshot `agent_step_instances`. An existing spawned step instance survives.
Optional caller variables must not collide with reserved persona state; task
context is not task claiming. Do not use optional parameters to bypass a gate.

To activate a whole definition, call `gobby-agents:apply_agent_definition`
with `agent` set to its name. This applies identity, prompt, rules, skills,
variables, tool blocks, and the step workflow to the caller's own session.
An activation that writes clears any persona overlay. An `unchanged` receipt
or a refusal writes nothing and keeps the overlay. To drop the overlay without
activating a definition, call `apply_persona(agent="default")`.

A session already bound to a non-default definition refuses another definition,
including a return to the base agent, with `role_change_requires_relaunch`.
Relaunch the terminal pane with the requested definition. Spawned sessions have
their definition fixed at spawn and refuse activation with
`spawned_session_definition_fixed`.

If resolution fails or the definition lacks the persona surface, report the
error and list eligible choices. Do not substitute a spawned worker. Check the
installed definition's scope and source filter when a name is unavailable.

Guide: [Usage surfaces](../../../../../../../../docs/guides/agents.md#usage-surfaces).

_Last verified: 2026-10-08_
