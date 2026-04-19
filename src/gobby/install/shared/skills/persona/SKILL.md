---
name: persona
description: Switch the current session to a persona-capable agent definition without spawning a child agent or changing provider/model/isolation. Use with `/gobby persona <name>`.
version: "1.0.0"
category: core
triggers: persona, roleplay, planner, architect
metadata:
  gobby:
    audience: interactive
    depth: 0
---

# /gobby persona

Switch the current session to a persona-capable agent definition.

This command is for conversational session switching, not autonomous agent spawning.
It updates the session persona and skill selection for the next user turn.

## Behavior

- Do not spawn an agent.
- Do not change provider, model, reasoning effort, or isolation.
- Use `gobby-agents.apply_persona` as the canonical operation.

## Workflow

1. If the user did not provide a persona name:
   - List available persona-capable definitions with `gobby-workflows.list_agent_definitions(surface_filter="persona")`.
   - Present the names and short descriptions.
   - Ask which persona to load.

2. If the user provided a persona name:
   - Call `gobby-agents.apply_persona(agent="<name>")`.
   - Report whether the switch succeeded.
   - Tell the user the updated persona will apply on the next turn.

3. If the requested definition is not persona-capable or does not exist:
   - Surface the error clearly.
   - Offer to list available personas.

## Notes

- `/gobby persona planner` should make the current session behave like the planner persona.
- `/gobby persona default` should restore the default conversational persona.
- If the user wants an autonomous worker, that is a different operation: use agent spawning tools instead.
