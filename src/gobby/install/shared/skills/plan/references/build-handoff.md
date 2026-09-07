# Build handoff

## Build Handoff States

Use build seed inputs for plan-file handoff:

- `planning_seed_state=drafted`: start at planning.
- `planning_seed_state=needs_review`: start at planning review with
  `completed_plan_review_rounds` already counted.
- `planning_seed_state=approved`: start directly at expansion.

`/gobby expand` remains available for manual expansion, debugging, and targeted
reruns. After approval, offer both manual expansion and `gobby build`.

Registration waits for a real implementation root. The existing
`gobby-plans:create_plan` API requires a real `root_task_ref` and generates the
initial coverage manifest for an implementation plan.
Never create a planning task merely to obtain a registry root.

For manual expansion, create or select the real epic that will own the expanded
work, register the canonical plan against that root through
`gobby-plans:create_plan`, then run expansion. For `gobby build`, let the build
service create or reuse its real implementation root; once that root is
available, the planning or expansion stage binds the canonical plan to it
through the same API. In either path, keep the canonical file as the sole
narrative authority and retain exactly one active registry row for that file and
root.

Unattended `gobby build` keeps its existing stage-manifest sequence. Its review
policy and configured round counts decide which deterministic stages run. Do
not inject interactive menus or reinterpret an omitted optional interactive
review as approval; the established approval gate and stage order remain in
force.
