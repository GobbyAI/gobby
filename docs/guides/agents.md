# Agents

Agents are typed definitions that describe either a current-session persona or
a spawned worker session. The same definition model works across supported CLIs;
provider-specific hooks are normalized before workflow rules evaluate.

For the broader control-plane model, see [Workflows Overview](./workflows-overview.md).

## Usage Surfaces

Agent definitions have explicit `surfaces`:

| Surface | Runtime tool | What happens |
| --- | --- | --- |
| `persona` | `gobby-agents:apply_persona` | Updates the current session's persona and active skill selection for the next user turn. |
| `spawn` | `gobby-agents:spawn_agent` or `dispatch_batch` | Starts a child session, records an agent run, and optionally creates or reuses isolation. |

`apply_persona` is intentionally narrow. It sets prompt-facing persona state,
skill selection, and reinjection flags; it does not change provider, model,
isolation, active rules, tool restrictions, or inline step workflow state.
The `prompts.persona` block is the complete interactive preamble.

Spawned runs use the full runtime path. They can inherit or override execution
settings, register inline step workflows, receive task/session variables, and
publish completion state back to waiting parents. Every spawn mode uses the
complete `prompts.agent` preamble.

Bundled `memory-lifecycle` templates provide shared policy for personas and spawned
agents; inspect installed enabled rows and selectors before assuming enforcement.
Agent definitions do not need to duplicate that policy. In task work,
`search-memories-on-claim` prompts search before editing. During planning,
`guard-plan-memory-writes` keeps provisional findings in plan evidence. After
closure, `review-closed-task-memories-before-compact` and
`review-closed-task-memories-on-stop` request one bounded review for the
closure batch.

## Definition Storage

Agent definitions are stored in `agent_definitions`. An optional one-to-one
`agent_step_workflows` child holds the nested `step_workflow` payload
(`steps`, `variables`, `exit_condition`). Runtime sessions snapshot that
child onto `agent_step_instances` at spawn. Turn-start reconciliation may
restore a missing snapshot only for a spawned or agent-run-backed session with
an assigned or active task. Persona activation never creates a step instance.
Definitions are managed through `gobby-workflows`.

Bundled definitions live in:

```text
src/gobby/install/shared/workflows/agents/
```

The bundled directory includes templates for planning, review,
writing, analysis, image generation, maintenance, merge work, and default
interactive use. Inspect installed rows for effective enablement and overrides.
Retired bundled agents are removed from this tree; sync
soft-deletes existing installed bundled rows when their YAML no longer exists.

Use these tools to inspect or change definitions:

- `gobby-workflows:list_agent_definitions`
- `gobby-workflows:get_agent_definition`
- `gobby-workflows:create_agent_definition`
- `gobby-workflows:toggle_agent_definition`
- `gobby-workflows:delete_agent_definition`
- `gobby-workflows:update_agent_rules`
- `gobby-workflows:update_agent_variables`
- `gobby-workflows:update_agent_step_workflow`

## Definition Shape

The current `AgentDefinitionBody` schema accepts these primary fields:

| Field | Purpose |
| --- | --- |
| `name` | Unique definition name |
| `description` | Human-readable summary |
| `sources` | Optional CLI-source filter |
| `surfaces` | `spawn`, `persona`, or both |
| `prompts.persona` | Complete interactive guidance for the `persona` surface |
| `prompts.agent` | Complete automated-run guidance for the `spawn` surface |
| `provider` | Provider override or `inherit` |
| `model` | Optional model override |
| `reasoning_effort` | Optional normalized reasoning effort string |
| `reasoning_required` | Whether unsupported reasoning should fail instead of warn |
| `fallback_agent` | Optional fallback definition for provider rotation |
| `api_base` / `api_token` | Optional custom model endpoint configuration |
| `isolation` | `none`, `worktree`, `clone`, or `inherit` |
| `base_branch` | Branch used for new isolation, or `inherit` |
| `timeout` | Runtime limit in seconds; `0` means unlimited |
| `workflows` | Rule, skill, variable, and pipeline selectors |
| `skills` | Metadata for baseline and allow-listed skill families |
| `blocked_tools` / `blocked_mcp_tools` | Definition-level restrictions |
| `step_workflow` | Optional nested object with `steps`, `variables`, and `exit_condition` |
| `enabled` | Whether the definition is active |

Every declared surface requires its prompt block. Legacy `role`, `goal`,
`personality`, and `instructions` fields are rejected with a migration hint.
Older YAML may contain a `mode` field; new definitions should use `surfaces`
plus the runtime tool choice instead.

## Strict Execution Fields

Several execution fields use strict YAML types:

- `model`
- `reasoning_effort`
- `reasoning_required`
- `fallback_agent`
- `api_base`
- `api_token`

Invalid:

```yaml
model: 1234
reasoning_effort: 2
reasoning_required: "false"
fallback_agent: 0
api_base: 12345
api_token: false
```

Valid:

```yaml
model: "gpt-5.6-sol"
reasoning_effort: xhigh
reasoning_required: false
fallback_agent: "qa-reviewer"
api_base: "http://localhost:1234/v1"
api_token: "${LM_STUDIO_API_KEY}"
```

`reasoning_effort` is normalized by the agent reasoning layer. Use the same
strings accepted by the current provider/model routing code.

## Minimal Definition

```yaml
name: docs-worker
description: Documentation implementation worker
surfaces: [spawn, persona]
provider: inherit
isolation: inherit
timeout: 1200

prompts:
  persona: |
    Help write and review Gobby documentation against the local source tree.
    Focus on accuracy, structure, terminology, and reader clarity.
  agent: |
    Claim the assigned docs task, audit the guide against code-owned sources,
    make a scoped docs change, run focused verification, commit, hand off the
    configured review stage, and end the agent run.

workflows:
  rule_selectors:
    include:
      - "tag:default"
      - "tag:worker-safety"
  variables:
    assigned_task_id: "#123"

step_workflow:
  variables:
    task_claimed: false
    review_submitted: false
  steps:
    - name: claim
      allowed_tools:
        - mcp__gobby__call_tool
        - mcp__gobby__list_mcp_servers
        - mcp__gobby__list_tools
        - mcp__gobby__get_tool_schema
      allowed_mcp_tools:
        - "gobby-tasks:claim_task"
        - "gobby-tasks:get_task"
      on_mcp_success:
        - server: gobby-tasks
          tool: claim_task
          action: set_variable
          variable: task_claimed
          value: true
      transitions:
        - to: implement
          when: "vars.task_claimed"

    - name: implement
      allowed_tools: "all"
      blocked_mcp_tools:
        - "gobby-tasks:reopen_task"
      on_mcp_success:
        - server: gobby-tasks-ops
          tool: submit_for_review
          action: set_variable
          variable: review_submitted
          value: true
      transitions:
        - to: finish
          when: "vars.review_submitted"

    - name: finish
      allowed_tools:
        - mcp__gobby__call_tool
        - mcp__gobby__list_mcp_servers
        - mcp__gobby__list_tools
        - mcp__gobby__get_tool_schema
      allowed_mcp_tools:
        - "gobby-agents:end_agent_run"
```

## Inline Step Workflows

A nested `step_workflow.steps` list constrains phased behavior for spawned
runs. Each step can define:

| Field | Purpose |
| --- | --- |
| `name` | Step identifier |
| `description` | Human-readable summary |
| `status_message` | Step-specific guidance shown to the session |
| `allowed_tools` / `blocked_tools` | Native tool restrictions |
| `allowed_mcp_tools` / `blocked_mcp_tools` | MCP tool restrictions such as `gobby-tasks:claim_task` |
| `on_enter` / `on_exit` | Actions around step boundaries |
| `on_mcp_before` / `on_mcp_success` / `on_mcp_error` | Handlers for MCP attempts or outcomes |
| `transitions` | Variable-driven step transitions |
| `exit_when` | Optional per-step exit condition |

Step restrictions are additive with the rule engine. A tool must satisfy both
the current step and the active rules.

## Lifecycle Model

Rules should be authored against semantic workflow events:

- `turn_start`
- `turn_end`

Raw `before_agent`, `after_agent`, and `stop` events are normalized runtime
details. Use them only when the distinction is the subject of the rule. In the
workflow engine, `turn_start` resolves from the pre-turn boundary, while
`turn_end` resolves from post-turn and stop boundaries.

Ending a chat turn is separate from ending a spawned agent run. A spawned worker
that has completed its workflow should call `gobby-agents:end_agent_run` so the
run is marked successful and completion subscribers are notified.

Use `gobby-agents:stop_agent` when a parent wants to cancel a pending or running
run. Use `gobby-agents:kill_agent` for targeted process termination and runtime
cleanup. `kill_agent` can also self-terminate with a status, but normal workflow
success should use `end_agent_run`.

## Runtime Tools

`gobby-agents` owns run execution, inspection, termination, persona application,
messaging, and command coordination.

Run tools:

- `spawn_agent`
- `dispatch_batch`
- `apply_persona`
- `get_agent_result`
- `get_agent_capture`
- `get_agent_live_output`
- `wait_for_agent`
- `wait_for_output`
- `checkpoint_agent_worktree`
- `cancel_stale_helpers`
- `list_agent_runs`
- `list_running_agents`
- `get_running_agent`
- `stop_agent`
- `kill_agent`
- `end_agent_run`
- `can_spawn_agent`
- `evaluate_spawn`
- `running_agent_stats`
- `unregister_agent`

Coordination tools:

- `send_message`
- `wait_for_coordination`
- `cancel_coordination_wait`
- `get_inter_session_message`
- `get_inter_session_messages`

For a coordinated hold, call `wait_for_coordination(owner_session="#123", ... )`
with exactly one condition: `coordination_key="unique-release-key"` or
`statuses=["paused", "completed"]`. The owner releases a keyed wait by sending
you a `coordination_release` message with `metadata.coordination_key` equal to
that key. Ordinary message text has no release or wake semantics.

The tool returns a durable `wait_id` and an `outcome` of `waiting`, `released`,
`status_matched`, `owner_ended`, `cancelled`, or `timeout`. Yield after `waiting`;
completion uses the existing durable mailbox and protected wake handling.
Expiry defaults to 900 seconds and accepts at most 3600 seconds. Repeating an
identical owner/condition registration returns the original wait without extending
its deadline, including its terminal outcome. Use a fresh unique release key for a
new hold. Only the waiting session can call `cancel_coordination_wait(wait_id=...)`.

`send_message` uses explicit targets: `global`, `project`, `parent`, `session`,
`agent`, and `build`. Spawned agents may omit `target`; it defaults to `parent`.
Other callers must pass `target`. `global` reaches every other live non-system
session owned by the sender's machine across projects. A targetless `project` send
reaches the same population in the sender's project and is the default for
repository coordination. System-originated
project sends must provide `project_id`; ordinary sessions derive their project and
must not override it. `session`, `agent`, and `build` require `target_id`.

Message text never triggers a wake. Set `wake=true` only when immediate processing is
intended; it may steer an active turn. Interrupted sessions and sessions awaiting input,
approval, or handoff retain the durable message without daemon input. A later mailbox
receipt, not a live trigger outcome, acknowledges delivery. Direct tmux interruption in
Qwen and AGY cannot be protected without positive provider or Gobby-mediated key/output
evidence, so unconfirmed sessions remain active.

For daemon restart coordination, queue the outage notice and explicitly wake after the
daemon is back:

```python
send_message(
    target="project",
    content="Daemon restart pending; save terminal drafts.",
    wake=False,
)

send_message(
    target="project",
    content="Daemon restart complete; continue.",
    wake=True,
)
```

## Blocked Child Communication

A `task_blocker` message must identify the assigned task in `metadata.task_id`
and use `target="parent"` (or omit `target`, which defaults to `parent` for
spawned agents). Spawned agents may send only to this target and
cannot override `from_session`. In configured worker step workflows, successful
delivery sets `blocker_handed_off` and advances to the termination step. The worker
still calls `end_agent_run` with a structured blocker handoff; sending a message
alone is not a universal process-exit operation. Inspect the installed definition
and active step for the applicable transition. The parent reads the handoff and
retained work before respawning with the answer and reusing the worktree. For a
question that keeps the child alive, use `message_type="message"`.

Spawn requests can pass `agent`, `task_id`, isolation fields, provider/model
overrides, reasoning fields, runtime limits, parent session, and project path.
`dispatch_batch` uses the same spawn machinery for multiple task suggestions.

## Recovery Checkpoints

The original parent can call `checkpoint_agent_worktree(run_id=...)` after its
child run is terminal. The task must remain open, with no active task/worktree
writer. The registered isolated task worktree must match its linked branch and
machine; foreign owners, foreign-attributed paths, and unattributed paths are
rejected. A successful checkpoint returns commit/path evidence and releases the
temporary worktree claim while preserving the task claim. It does not validate
or close the task. If a release error includes a commit, inspect it before retrying.

## Isolation

Isolation is a runtime setting for spawned runs:

| Isolation | Behavior |
| --- | --- |
| `none` | Work in the caller's current repository context |
| `worktree` | Create or reuse a git worktree with separate branch state |
| `clone` | Use a separate clone for stronger filesystem isolation |
| `inherit` | Definition-only; resolves to `none` unless the spawn passes `isolation` |

Docs leaf work may run inside a parent epic's existing isolation context. In
that case the agent definition can keep `isolation: inherit`, and dispatch
provides the concrete worktree or clone context.

## Recommended Patterns

- Put reusable safety policy in rules; keep agent definitions focused on role,
  selectors, runtime settings, and step flow.
- Use `surfaces` to make persona-capable definitions explicit.
- Keep interactive domain guidance in `prompts.persona`; keep assigned-task and
  run-lifecycle instructions in `prompts.agent`.
- Seed only variables the agent owns, such as `assigned_task_id` or
  stage-specific gates.
- Use inline steps for lifecycle phases: claim, load required skills,
  implement, review handoff, finish.
- Use `end_agent_run` as the explicit successful termination path for spawned
  workflows.
- Treat removed bundled agent YAML as retired; sync prunes the existing installed row.

## Related Guides

- [Workflow Rules](./workflow-rules.md) for semantic rule events
- [Rules](./rules.md) for hook-time enforcement
- [Pipelines](./pipelines.md) for deterministic automation
- [Orchestration](./orchestration.md) for stage dispatch and review flow

_Last verified: 2026-09-12_
