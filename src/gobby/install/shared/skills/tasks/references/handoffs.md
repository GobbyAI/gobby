# Task and session boundaries

Choose the boundary from the task state, then write a readable structured
handoff derived from the provider-native tracker.

| Situation | Required action |
| --- | --- |
| Active task reaches context pressure | `set_handoff(clear_session=false)` |
| Root/coordinator finishes a task or moves between epic children | `set_handoff(clear_session=true)` |
| Spawned worker finishes or completes a blocker handoff | Structured `end_agent_run(...)` |

State what is true now and give the receiving coordinator concrete next actions.
Include decisions, blockers, commands, diagnostics, paths, impact, and references
only when they help the receiver continue.

Do not paste cumulative history, previous handoffs, raw logs, completed ledgers,
or artificial shorthand. Do not create a checkpoint file by default; checkpoint
files remain available only when the user explicitly requests one.

`set_handoff` remains uncapped and its `clear_session` boolean is the boundary
control. A spawned worker supplies nonblank `current_state` and at least one
nonblank coordinator action in `next_steps` before cooperative success or a
`task_blocker` exit. Forced kills and crashes may have no authored handoff.
