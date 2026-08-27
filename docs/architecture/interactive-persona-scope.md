# Interactive Persona Scope Decision

Status: accepted. Owner task: `#21063`. Decision date: 2026-08-26.
Implementation task: `#21088`.

## Decision

Applying an interactive persona inherits only the agent definition's persona prompt
and skill selection. It does not inherit the definition's rule selectors,
`agent_scope` identity, variables, blocked tools, blocked MCP tools, step workflow,
provider, model, or isolation settings.

Lifecycle and enforcement identity remains separate from persona identity:

- `_agent_type` identifies the default or spawned agent lifecycle. Rule
  `agent_scope` matching continues to use this value.
- Persona identity is prompt-facing state. Prompt injection resolves it separately,
  and skill selection updates through `_active_skill_names` and `_skill_format`.
- Applying or switching a persona preserves the session's existing active rules,
  tool restrictions, spawned status, and step-workflow state.

Explicit metadata supplied by the caller remains an input to the operation; it does
not cause the persona definition's lifecycle or enforcement configuration to load.

## Rationale

An interactive operator may adopt a specialist perspective while retaining control
of the session. Spawned-worker restrictions describe autonomous execution posture,
so deriving them from a prompt persona unexpectedly changes operator permissions.

The reported failure was an interactive session applying `qa-reviewer`, which made
`_agent_type` match `qa-reviewer` and activated `no-push-for-workers` plus the
worker-safety rule family. Separating the identities preserves worker enforcement for
spawned runs while keeping persona application limited to prompt and skills.

## Implementation

Task `#21088` owns the runtime and focused-test changes. Its acceptance criteria cover
prompt selection, skill updates, `agent_scope` isolation, rule-selector isolation,
and session reconciliation.
