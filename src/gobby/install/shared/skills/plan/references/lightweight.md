# Lightweight planning

## Lightweight Workflow

Produce a conversational, decision-complete plan. Keep concrete deliverables,
dependencies, file or subsystem targets, validation, risks, and explicit
out-of-scope boundaries.

Lightweight depth is artifact-free. Do not load `plan-draft`, create or update a
file under `.gobby/plans/`, run `gobby plans validate`, or offer build handoff.
Enhancement and adversarial review also belong to Full planning.

Present the plan directly in the conversation, then use a compact checkpoint:
approve it for implementation, `continue interactively`, or switch to Full.
`continue interactively` refines the conversational plan and returns to the same
checkpoint. Switching to Full begins the artifact workflow from the confirmed
Decision Record.
