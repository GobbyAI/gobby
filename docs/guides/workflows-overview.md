# Workflows Overview

Gobby's workflow system is the control plane that keeps sessions, agents, and
automation aligned with the repo's rules. It is **CLI agnostic**: Claude,
Codex, and Gemini sessions all flow through the same normalized hook events,
session variables, rule engine, and MCP tool surface.

This guide is the high-level map. Use it to decide whether a behavior belongs
in a rule, an agent definition, a pipeline, or an orchestration flow.

## Mental Model

Gobby has four layers that compose together:

| Layer | Purpose | Where it lives | Runtime surface |
| --- | --- | --- | --- |
| Rules | Enforce invariants on hook events | `workflow_definitions` / bundled YAML | `gobby-workflows` + rule engine |
| Agents | Define persona, restrictions, and step workflows | `workflow_definitions` / bundled YAML | `gobby-workflows` definitions, `gobby-agents` runtime |
| Pipelines | Run deterministic multi-step automation | `workflow_definitions` / bundled YAML | `gobby-workflows` pipeline tools |
| Orchestration | Coordinate tasks, agents, isolation, and completion | Pipeline + task/agent tooling | `gobby-workflows`, `gobby-tasks`, `gobby-agents`, `gobby-worktrees`, `gobby-clones`, `gobby-merge` |

The shared state across all four layers is:

- Session variables, which rules and step workflows read and mutate.
- Workflow definitions, which live in the database and are synced from bundled
  or project YAML.
- Completion events, which let agents and pipelines wake a waiting parent flow.

## How The Pieces Fit

### Rules

Rules are reactive. They fire on semantic workflow events such as
`turn_start`, `turn_end`, `before_tool`, `after_tool`, and `session_start`,
plus raw normalized escape-hatch events when needed.

Rules are the right tool when you need to:

- Block a tool call or stop attempt.
- Rewrite tool input before execution.
- Inject guidance into the session context.
- Set or track session variables.
- Trigger MCP side effects in response to hooks.

Rules do **not** decide the broader workflow. They enforce local invariants.

### Agents

Agents are reusable worker definitions. They combine:

- Persona and prompt fields such as `role`, `goal`, `personality`, and `instructions`.
- Provider and isolation preferences.
- Rule selectors, variable overrides, and optional inline step workflows.
- Tool restrictions that apply either globally or per step.

An agent definition can be applied to the current session with
`gobby-agents:apply_persona`, or used to spawn a child session with
`gobby-agents:spawn_agent` or `dispatch_batch`.

### Pipelines

Pipelines are deterministic automation. They execute ordered steps such as:

- `exec` for shell commands
- `prompt` for LLM reasoning
- `mcp` for direct tool calls
- `invoke_pipeline` for nested pipelines
- `activate_workflow` for step-workflow activation inside pipeline execution
- `wait` for completion-event blocking

Pipelines are the right tool when you need explicit sequencing, resumability,
approval gates, or non-interactive orchestration.

### Orchestration

Orchestration is not a separate legacy server anymore. In current Gobby,
orchestration is built by composing:

- Task state from `gobby-tasks`
- Agent runtime from `gobby-agents`
- Deterministic control flow from `gobby-workflows` pipelines
- Isolation from `gobby-worktrees` or `gobby-clones`
- Landing and conflict handling from `gobby-worktrees`, `gobby-clones`, and `gobby-merge`

The bundled `orchestrator` and `dev-orchestrator` pipelines are the canonical
examples.

## Current Public Surface

These are the servers readers should think in terms of when authoring workflow
behavior today:

| Server | What it owns |
| --- | --- |
| `gobby-workflows` | Workflow, rule, variable, agent-definition, and pipeline definitions; pipeline execution and completion waiting |
| `gobby-agents` | Agent spawning, runtime inspection, persona application, inter-agent messaging, and commands |
| `gobby-tasks` | Task lifecycle, dependencies, readiness, and review states |
| `gobby-worktrees` | Worktree creation, sync, merge, and cleanup |
| `gobby-clones` | Clone-based isolation lifecycle |
| `gobby-merge` | AI-assisted conflict resolution for merge flows |

## Decision Guide

| You need to... | Put it in... | Why |
| --- | --- | --- |
| Block `git push`, destructive shell, or invalid task lifecycle actions | Rule | Reactive enforcement belongs at hook time |
| Inject reminders or dynamic context into the next agent turn | Rule | `inject_context` and `load_skill` are event-driven |
| Guide a worker through claim → implement → terminate | Agent | Inline step workflows model phased behavior |
| Spawn child workers for ready tasks | Pipeline or orchestration flow | Dispatch is deterministic control flow |
| Wait for a spawned worker or nested run to finish | Pipeline | `wait` steps and `wait_for_completion` exist for this |
| Keep work moving on a cron/tick loop | Pipeline orchestration | Tick-based pipelines are the current orchestration model |

## Event Flow

At runtime, the control flow looks like this:

1. A CLI session starts or a child session is spawned.
2. Gobby resolves the session's persona, active rules, variables, and skills.
3. Hook events fire as the session works: tool calls, model requests, stop
   attempts, compaction, notifications, and more.
4. The rule engine evaluates matching rules in priority order and returns a
   merged response: allow/block, context injections, rewritten input, variable
   updates, and deferred MCP calls.
5. Pipelines or parent sessions use MCP tools to spawn agents, wait for
   completion, inspect task state, and continue orchestration.

Two important abstractions here are `turn_start` and `turn_end`:

- `turn_start` is the semantic workflow event for the beginning of a turn.
- It fires alongside the raw `before_agent` hook.
- `turn_end` is a semantic workflow event.
- It fires alongside the raw hook event when a session reaches the end of a
  turn, whether that arrives as `after_agent` or `stop`.
- That keeps stop gates and end-of-turn policies consistent across supported
  CLIs.

## Definitions vs Runtime State

Keep this split clear:

- **Definitions** are reusable YAML-backed objects synced into
  `workflow_definitions`.
- **Runtime state** is session-specific: variables, active workflow instances,
  agent runs, pipeline executions, completion subscriptions, and task claims.

When a guide says "enable a rule" or "update an agent definition", that is a
definition change. When it says "the step transitioned" or "the pipeline is
waiting for approval", that is runtime state.

## Recommended Reading

- [Rules](./rules.md) for the event model and effect types
- [Agents](./agents.md) for agent definitions, isolation, and step workflows
- [Pipelines](./pipelines.md) for execution semantics and pipeline tools
- [Orchestration](./orchestration.md) for the current task/agent coordination model
- [Rule Authoring Guide](./workflow-rules.md) for engine caveats and safety rules
