# Variables

Load this overview when choosing how to inspect or change variables. Definitions,
session values and agent-step values are distinct stores.

Discover definitions through `gobby-workflows:list_variables` (schema first).
Use top-level `get_variable(session_id=...)` for effective session values.
Before mutation, load the operation topic and confirm ownership, scope and types.

- `$gobby variables references definitions` — definition lifecycle.
- `$gobby variables references values` — session reads and writes.
- `$gobby variables references defaults` — defaults and precedence.
- `$gobby variables references scope` — project/global/session/step boundaries.
- `$gobby variables references overrides` — user configuration and persistence.
- `$gobby variables references resets` — recovery and lifecycle reset constraints.

A menu does not run these operations or load their references. Tool schemas remain
authoritative. If a value is surprising, compare definition scope/enabled state
against the intended session before writing. See the [verified guide](../../../../../../../../docs/guides/variables.md).
