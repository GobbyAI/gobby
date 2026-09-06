# Lightweight planning

## Lightweight Workflow

Produce a conversational, decision-complete plan. Keep concrete deliverables,
dependencies, file or subsystem targets, validation, risks, and explicit
out-of-scope boundaries.

Lightweight depth is artifact-free. Do not load `plan-draft`, create or update a
file under `.gobby/plans/`, run `gobby plans validate`, or offer build handoff.
Enhancement and adversarial review also belong to Full planning.

Before presenting, run a mechanism audit: identify every new subsystem,
dependency, abstraction, configuration surface, and paid-operation loop. Remove
each mechanism that is unnecessary for complete acceptance coverage. Preserve
mechanisms required for correctness, security, or an explicit acceptance case.

Present the plan directly in the conversation, then use a compact checkpoint:
approve it for implementation, `continue interactively`, or switch to Full.
`continue interactively` refines the conversational plan and returns to the same
checkpoint. Switching to Full begins the artifact workflow from the confirmed
Decision Record.
