# Build handoff

## Build Handoff States

Use build seed inputs for plan-file handoff:

- `planning_seed_state=drafted`: start at planning.
- `planning_seed_state=needs_review`: start at planning review with
  `completed_plan_review_rounds` already counted.
- `planning_seed_state=approved`: start directly at expansion.

`/gobby expand` remains available for manual expansion, debugging, and targeted
reruns. Full planning offers approved artifacts to `gobby build` only after the
user approves the optional handoff.
