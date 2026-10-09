# Agents

Agents are typed definitions that describe either a current-session persona or
a spawned worker session. The same definition model works across supported CLIs;
provider-specific hooks are normalized before workflow rules evaluate.

For the broader control-plane model, see [Workflows Overview](./workflows-overview.md).

## Usage Surfaces

Single managed launches accept an optional network profile: MCP `spawn_agent`
and `POST /api/agents/spawn` use `network: "none" | "trusted" | null`; the CLI
uses `gobby agents spawn "Work" --session <ref> --network none|trusted`.
Omission or null inherits the final agent definition, including a selected
fallback. An explicit value overrides that final definition for one launch.
The stored definition and later launches keep their original profile; resume
uses the effective sandbox configuration saved for the selected launch.

`trusted` remains an SRT allowlist, with the vendored Trusted domains, Git
forges, and package registries. It never grants unrestricted network access.
Root sessions and the local operator may select either profile. Spawned callers
may override only when their verified run names `default` or `orchestrator`;
other callers may launch with the inherited profile. The caller guard also
enforces this on pipeline MCP steps. Daemon-internal callers cannot override.
Spawn scope and seat-spawn restrictions still apply. The direct HTTP route
requires operator credentials and rejects explicit network for `web_chat`.

Until batch spawning is retired, the operator-only `POST /api/agents/spawn/batch`
also accepts `network` on each item, with the same inheritance, one-launch
lifetime, validation, and `web_chat` refusal as single HTTP spawns.

Agent definitions have explicit `surfaces`:

| Surface | Runtime tool | What happens |
| --- | --- | --- |
| `persona` | `gobby-agents:apply_persona` or `gobby-agents:apply_agent_definition` | Switches the persona prompt and skills live, or activates the whole definition on the current session. |
| `spawn` | `gobby-agents:spawn_agent` or `dispatch_batch` | Starts a child session, records an agent run, and optionally creates or reuses isolation. |

`apply_persona` is intentionally narrow. It sets prompt-facing persona state,
skill selection, and reinjection flags; it does not change provider, model,
isolation, active rules, tool restrictions, or inline step workflow state.
The `prompts.persona` block is the complete interactive preamble.

### Definition Activation

Use `gobby-agents:apply_agent_definition(agent="<name>")` to activate a whole
definition on the caller's own session. It applies identity, prompt, rule and
skill selection, variables, tool blocks, and any step workflow. It requires a
`persona` surface. A definition with `workflows.pipeline` is refused with
`pipeline_requires_spawn`; use the spawn surface for that definition.

Activation stores `_agent_type` and the content pin `_agent_definition_hash`
alongside the selected rules, skills, variables, and tool restrictions. The
configured base agent can activate a seat. The same definition with the same
pin returns `status: unchanged`: it writes nothing, reinjects nothing, and
ignores optional `variables` and `task_id`. Task context never claims a task.
An unknown definition, missing surface, unresolved task, or variable collision
is refused before writing.

A different definition on an already seated terminal, including a return to
the base agent, is refused with `role_change_requires_relaunch`. Relaunch the
pane with the requested definition. Web-chat agent changes restart the CLI
process on the conversation's existing session row and apply the new definition;
definition-owned variables and the previous step instance are replaced, while
runtime conversation variables remain. Spawned sessions refuse activation with
`spawned_session_definition_fixed`: their definition is fixed at spawn.

`apply_persona` remains a live overlay. Its prompt, skill selection, and skill
format survive compaction and resume while the seat's rules, variables, tool
blocks, and step workflow remain in force. `apply_persona(agent="default")`
removes the overlay and restores the active definition's own prompt and skills
(or the default persona on a base-agent session). A definition activation that
writes clears the overlay; an `unchanged` receipt or refusal keeps it.

On later same-seat activation, a changed content pin applies the current
definition, stores the new pin, and reports one drift line on the next turn.
An existing step instance keeps its current step and snapshot until that unit
of work ends. A failed initial step-instance save is repaired by reconciliation
on a later hook event.

| Boundary | Definition and runtime continuity |
| --- | --- |
| Compaction | Same session; reactivation by `_agent_type` preserves the seat and running step instance. A persona overlay keeps its prompt and skills. |
| Pane resume | Reactivates the same session and preserves its running step instance, as compaction does. |
| Staged `/clear` | The successor inherits `_agent_type` and `_agent_definition_hash`, then starts a fresh step instance at the current definition's first step. A spawned interactive seat's live run, terminal binding, and run back-pointer move to the successor; the pane's managed identity addresses that successor. |
| Spawned-run resume | Reuses the session row and persisted launch snapshot; the spawned definition remains fixed. |

Run lifetime is separate from activation. `execution_mode` and
`idle_ttl_seconds` affect spawned runs; activating a hand-launched pane writes
neither. See [Lifecycle Model](#lifecycle-model) for standing seats and TTLs.
The activation implementation is
[`apply_agent_definition.py`](../../src/gobby/mcp_proxy/tools/apply_agent_definition.py).

Spawned runs use the full runtime path. They can inherit or override execution
settings, register inline step workflows, receive task/session variables, and
publish completion state back to waiting parents. Every spawn mode uses the
complete `prompts.agent` preamble.

Bundled `memory-lifecycle` templates provide shared policy for personas and spawned
agents; inspect installed enabled rows and selectors before assuming enforcement.
Agent definitions do not need to duplicate that policy. In task work, the
`surface-memories-*` rules inject a bounded memory index at a parent turn start,
a spawn, a claim, and a handoff read, before editing starts. During planning,
`guard-plan-memory-writes` keeps provisional findings in plan evidence. After
closure, `review-closed-task-memories-before-handoff` and
`review-closed-task-memories-on-stop` request one bounded review for the
closure batch.

## Definition Storage

Agent definitions are stored in `agent_definitions`. An optional one-to-one
`agent_step_workflows` child holds the nested `step_workflow` payload
(`steps`, `variables`, `exit_condition`). Runtime sessions snapshot that
child onto `agent_step_instances` at spawn or whole-definition activation.
Reconciliation restores a missing instance whenever the active definition
declares steps, including interactive sessions without a task. A persona
switch never creates a step instance.
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
That removal is reversible: sync records it on the row, so returning the YAML
restores the row on the next sync even when its content is unchanged. A row you
delete yourself stays deleted across syncs.

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
| `version` | Optional human-readable string stored inside `definition_json` |
| `sources` | Optional CLI-source filter |
| `surfaces` | `spawn`, `persona`, or both |
| `prompts.persona` | Complete interactive guidance for the `persona` surface |
| `prompts.agent` | Complete automated-run guidance for the `spawn` surface |
| `execution_mode` | `one_shot` (default) or `interactive` for a standing spawned seat |
| `idle_ttl_seconds` | Optional positive integer, valid only with `execution_mode: interactive`; expires an idle spawned seat |
| `provider` | Provider override or `inherit` |
| `model` | Optional model override |
| `reasoning_effort` | Optional normalized reasoning effort string |
| `reasoning_required` | Whether unsupported reasoning should fail instead of warn |
| `fallback_agent` | Optional fallback definition for provider rotation |
| `api_base` / `api_token` | Optional custom model endpoint configuration |
| `checkout_mode` | `none`, `worktree`, `clone`, or `inherit` |
| `prewarm_pre_commit_store` | Whether a sandboxed spawn copies the pre-commit hook store into its run cache (default `true`); set `false` for definitions that never commit |
| `base_branch` | Branch used for new isolation, or `inherit` |
| `timeout` | Runtime limit in seconds; `0` means unlimited |
| `workflows` | Rule, skill, variable, and pipeline selectors |
| `blocked_tools` / `blocked_mcp_tools` | Definition-level restrictions |
| `spawnable_agents` | What a spawned run of this definition may spawn: `["*"]` any agent, named agents, or empty (the default) none. See [Spawn Scope](#spawn-scope) |
| `send_message_targets` | `send_message` target modes a spawned agent may use (default `["parent"]`), enforced by the `scope-spawned-agent-send-message` rule |
| `step_workflow` | Optional nested object with `steps`, `variables`, and `exit_condition` |
| `enabled` | Whether the definition is active |

Every declared surface requires its prompt block. Legacy `role`, `goal`,
`personality`, `instructions`, and the top-level `skills` map are rejected.
Use `workflows.skill_selectors` for discovery and
`step_workflow.variables.required_skills` for required loads. Use `surfaces`
plus the runtime tool choice instead of the retired `mode` field.

### Versioning

`version` is a human marker inside `definition_json`, not a separate identity
or database column. Quote it in YAML, for example `version: "1.0"`.
Activation pins the resolved body's content hash through
[`compute_definition_hash`](../../src/gobby/storage/definitions/_shared.py).
A version bump changes that hash like any other body edit. Bundled sync refreshes
managed definition drift; subsequent same-seat activation reports the new pin.

## Seats

Standing seats share the same typed definition shape. The current bundled
catalogue is pinned by
[`test_seat_definitions.py`](../../tests/workflows/test_seat_definitions.py):

| Definition | Responsibility |
| --- | --- |
| `assistant` | Communicates with the user and coordinates decisions |
| `orchestrator` | Assigns and coordinates repository work |
| `lane-manager` | Routes lane work, relays landings, and releases closes |
| `developer` | Implements one claimed task at a time in its assigned lane |
| `code-reviewer` | Reviews candidates, lands approved commits, and reports retests |
| `researcher` | Answers bounded research requests |
| `archivist` | Maintains the operational digest |
| `log-monitor` | Monitors health and admits heavy work and close reviews |
| `inbox-manager` | Consolidates lane reports for the Orchestrator |
| `merge-manager` | Tracks landed sources and pending activation evidence |

These templates are global installed definitions tagged `gobby` in storage;
their YAML `seat` tag marks the file-based catalogue. They declare both
`spawn` and `persona` surfaces, share one anchored prompt between the two,
inherit provider and checkout mode, omit a model override, and use
`execution_mode: interactive` with `timeout: 0`. Definitions never select a
terminal backend. The researcher additionally declares `idle_ttl_seconds: 900`.

Shared seat guidance is injected by the `roles` rules. Spawn and pipeline-launch
restrictions prevent seats from bypassing their assigned role; the automatic
task-close reviewer is the permitted managed review path. Definitions declare
tool blocks; path-aware rules constrain seats with bounded write scopes.
Seats with required skills or a fixed loop declare a step workflow. Message-driven
seats need no empty serve loop. Inspect installed rows and selectors for effective
policy; editing a bundled YAML alone does not activate it.

All lanes use the single `developer` definition. After claiming, it reads the
task and routes additional skills by the touched paths:

| Task paths | Additional skills |
| --- | --- |
| `web/` | `impeccable`, `typescript` |
| `crates/` | `rust` |
| Python only | No addition beyond the required skill set |
| Docs only | `tech-writer` |

Task-specific skills and named methodologies are added; `tdd:required` adds
`test-driven-development`. Required loads precede implementation. Lane identity
comes from task and queue ownership and coordinator instructions, rather than a
separate developer definition per language. Seat continuity is prompt guidance,
not a schema field: seats compact in place under context pressure. Developers,
researchers, and code reviewers also compact between units of work. Task
completion does not end the standing seat.

### Planning Runbook Agents

`plan-writer`, `plan-enhancer`, and `plan-adversary` are run-scoped planning
agents, separate from the standing-seat catalogue. The planning runbook launches
them; they complete their run through `end_agent_run`. Their own definitions
control provider, lifetime, and workflow choices, so standing-seat invariants
do not apply to them.

Build routing still names definitions whose bodies now serve standing seats.
Stages that depend on those bodies terminating can degrade. That limitation is
accepted during build retirement; this catalogue does not supply compatibility
one-shot copies. See [Orchestration](./orchestration.md) for dispatch behavior.

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
checkout_mode: inherit
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

A nested `step_workflow.steps` list constrains phased behavior for spawned runs
and activated interactive sessions. Each step can define:

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

A few tools bypass the native allow-list because they carry no capability of
their own. Provider tool-catalog tools (Claude Code's `ToolSearch`, Grok's
`search_tool`) discover tools without executing any, and skip a step's tool
checks entirely. Grok's `get_command_or_subagent_output` is a read-only poll
that returns the result of a call the step already allowed or denied, so it
passes a step's native allow-list; without the exemption a run that polled it
inside an MCP-only step collected identical `step-native-tool-allowlist`
denials until it was killed at the third one. A step that names the poll in
`blocked_tools` still blocks it.

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

For a standing seat, pass `execution_mode="interactive"` to `spawn_agent`, or set
`execution_mode: interactive` in its agent definition. The spawn argument overrides
the definition. Use `timeout: 0` for an unlimited lifetime. The resolved mode is
persisted in the run's launch snapshot and survives daemon-stop resume.
An explicit `spawn_agent(resume_session_id=...)` defaults to interactive when
`execution_mode` is omitted, including when the definition defaults to one-shot.
Pass `execution_mode="one_shot"` explicitly to resume as a finite worker.

Interactive seats remain available at idle prompts, after task closure, and after
runbook completion. Completing a runbook releases its step instance and dispatch
mutex while retaining the seat. Without an idle TTL, the watchdog does not
reprompt, recover, complete, or fail a seat for idleness or missing `end_agent_run`.
End a seat explicitly with `end_agent_run`, `stop_agent`, or `kill_agent`; process-exit cleanup
still applies. Provider quota exhaustion, terminal provider errors, and a full context
window still fail a seat; a provider capacity error gets bounded continue prompts that
never ask the seat to end its run. One-shot runs retain completed-turn recovery and bounded idle
reprompts. This lifetime choice is separate from provider launch options such as
`droid_mode`.

An interactive definition may set a positive `idle_ttl_seconds`. Spawn persists
it only when the effective mode is interactive; a one-shot override drops it.
At expiry, the watchdog uses its wrap, handoff, and end ladder. A coordinator-owned
wait holds the seat ahead of TTL expiry. Interactive seats with no TTL remain
available while idle. These exceptions do not bypass provider-failure or
dead-terminal cleanup.

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
- `apply_agent_definition`
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
with exactly one condition: `coordination_key="unique-release-key"`,
`statuses=["paused", "completed"]`, or `reply=True`. The owner releases a keyed
wait by sending you a `coordination_release` message with
`metadata.coordination_key` equal to that key. Ordinary message text has no
release or wake semantics for a keyed wait.

A reply wait is how a spawned worker waits for an ordinary answer it asked its
parent for. It resolves on the next durable message the owner session sends the
waiter after registration, whatever its type; `owner_session` defaults to the
caller's parent. Resolution is commit-ordered, so a message that was already
sent when the wait registered never resolves it — send the question and register
the wait in the same turn. While the wait is registered, the idle check and the
autonomous stuck sweep leave the run alone, and normal handling resumes once it
resolves, times out, or is cancelled.

The tool returns a durable `wait_id` and an `outcome` of `waiting`, `released`,
`replied`, `status_matched`, `owner_ended`, `cancelled`, or `timeout`. Yield after
`waiting`; completion uses the existing durable mailbox and protected wake handling.
Expiry defaults to 900 seconds and accepts at most 3600 seconds. While waiting,
repeating the same owner and condition returns the original wait and expiry.
After completion, repeating the same owner and condition creates a new wait.
Preserve `wait_id` and inspect its terminal outcome before registering again.
Use a fresh key for a separate keyed hold. Only the waiting session can call
`cancel_coordination_wait(wait_id=...)`.

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
approval, or handoff retain the durable message without daemon input. Before typing
into a terminal, the daemon probes the composer through the provider's detection
manifest; a composer that positively shows an operator draft is left alone and the
wake result carries `skipped: "composer_occupied"` (same bucket as `session_active`:
the message is persisted and the hook piggyback injects it on the session's next
turn). The probe reads a styled snapshot, so faint text (Claude Code's prompt
suggestion, the Codex and Droid placeholders) reads as an empty composer.
`priority="urgent"` bypasses the probe. A later mailbox
receipt, not a live trigger outcome, acknowledges delivery. Direct tmux interruption in
Antigravity cannot be protected without positive provider or Gobby-mediated key/output
evidence, so unconfirmed sessions remain active.

For daemon restart coordination, queue the outage notice and explicitly wake after the
daemon is back:

```python
send_message(
    target="project",
    content="Daemon restart pending.",
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
spawned agents). Spawned agents cannot override `from_session`, and the bundled
rule `scope-spawned-agent-send-message` limits each one to the target modes its
agent definition lists in `send_message_targets`. The default is `["parent"]`,
so this target is the only one an undeclared agent can use. Disabling the rule
lifts the limit. In configured worker step workflows, successful
delivery sets `blocker_handed_off` and advances to the termination step. The worker
still calls `end_agent_run` with a structured blocker handoff; sending a message
alone is not a universal process-exit operation. Inspect the installed definition
and active step for the applicable transition. The parent reads the handoff and
retained work before respawning with the answer and reusing the worktree. For a
question that keeps the child alive, use `message_type="message"`.

Spawn requests can pass `agent`, `task_id`, isolation fields, provider/model
overrides, reasoning fields, runtime limits, parent session, and project path.
`dispatch_batch` uses the same spawn machinery for multiple task suggestions.

## Spawn Scope

The bundled `limit-spawnable-agents` rule (tool-hygiene, tagged `default`)
limits what a spawned agent may spawn. A root session (no agent run, depth 0)
spawns any agent. A spawned agent's `spawn_agent` and `dispatch_batch` calls
follow the `spawnable_agents` of the installed definition its agent run names.
Each definition declares one of three things:

| Value | The spawned agent may spawn |
| --- | --- |
| `["*"]` | Any agent |
| `[merge-worker]` | Only the named agents |
| `[]` or omitted | No agent |

`"*"` stands alone; mixing it with names fails validation. Every agent the call
can start must be allowed, or the whole call is refused:

- An omitted `agent` counts as the tool's default (`default` for `spawn_agent`,
  `developer` for `dispatch_batch`).
- A `dispatch_batch` suggestion's own non-blank `agent` replaces the top-level
  one for that suggestion. `suggestions` must be a list of objects.
- Every agent in a target's `fallback_agent` chain counts, as `spawn_agent`
  walks it: up to five hops, stopping at a cycle or a missing definition.

A caller, run or definition the rule cannot resolve refuses the spawn. Disabling
the installed rule row lifts the limit; there is no hardcoded allowlist. The
agent depth limit applies independently. Pipeline `mcp` steps and daemon-driven
spawns (build dispatch, close validation) do not pass through `before_tool`, so
the rule does not govern them.

## Recovery Checkpoints

The original parent can call `checkpoint_agent_worktree(run_id=...)` after its
child run is terminal. The task must remain open, with no active task/worktree
writer. The registered isolated task worktree must match its linked branch and
machine; foreign owners, foreign-attributed paths, and unattributed paths are
rejected. A successful checkpoint returns commit/path evidence and releases the
temporary worktree claim while preserving the task claim. It does not validate
or close the task. If a release error includes a commit, inspect it before retrying.

## Checkout Modes

Isolation is a runtime setting for spawned runs:

| Isolation | Behavior |
| --- | --- |
| `none` | Work in the caller's current repository context |
| `worktree` | Create or reuse a git worktree with separate branch state |
| `clone` | Use a separate clone for stronger filesystem isolation |
| `inherit` | Definition-only; resolves to `none` unless the spawn passes `checkout_mode` |

Docs leaf work may run inside a parent epic's existing isolation context. In
that case the agent definition can keep `checkout_mode: inherit`, and dispatch
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

_Last verified: 2026-10-08_
