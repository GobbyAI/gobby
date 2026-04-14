# Hook Normalization V2

## Summary

This plan keeps the public workflow authoring API in snake_case and makes the
semantic layer explicit instead of pushing rule authors toward Claude-native
hook names.

The core decisions are:

1. Keep workflow and rule authoring on normalized snake_case event names.
2. Add `turn_start` as the semantic counterpart to the existing `turn_end`.
3. Keep raw normalized lifecycle events such as `before_agent`, `after_agent`,
   and `stop` as escape hatches for provider-specific behavior.
4. Do not rename adapters, hook handlers, or installer output to Claude-native
   names just to mirror provider docs.
5. Do not add a new generic executable event-trigger syntax to agent YAML in
   v2. Agent workflows remain step-driven; lifecycle enforcement remains in the
   rule engine. Agent YAML changes are instruction, contract, and status-message
   corrections.

## Public Event Model

### Default Semantic Events

These are the events rule authors should use by default:

| Event | Meaning |
| --- | --- |
| `session_start` | Session bootstrap, resume, clear, or compaction re-entry |
| `session_end` | Session teardown |
| `turn_start` | Portable start-of-turn boundary across Claude, Codex, and Gemini |
| `turn_end` | Portable end-of-turn boundary across Claude, Codex, and Gemini |
| `before_tool` | Before a native or MCP tool runs |
| `after_tool` | After a native or MCP tool completes |
| `pre_compact` | Before context compaction |
| `notification` | Notification-style event |

### Raw Normalized Escape-Hatch Events

These remain available when a rule truly needs provider-specific lifecycle
detail instead of semantic portability:

| Event | Meaning |
| --- | --- |
| `before_agent` | Raw normalized pre-turn hook |
| `after_agent` | Raw normalized post-turn hook |
| `stop` | Raw idle/stop hook from providers that expose it |
| `before_tool_selection` | Gemini-only tool-selection hook |
| `before_model` | Gemini-only model-start hook |
| `after_model` | Gemini-only model-end hook |
| `subagent_start` | Claude-native subagent start hook |
| `subagent_stop` | Claude-native subagent stop hook |
| `permission_request` | Claude-native permission hook |

### Cross-CLI Mapping

#### Claude

| Native Hook | Raw Workflow Event | Semantic Workflow Event |
| --- | --- | --- |
| `session-start` | `session_start` | `session_start` |
| `session-end` | `session_end` | `session_end` |
| `user-prompt-submit` | `before_agent` | `turn_start` |
| `pre-tool-use` | `before_tool` | `before_tool` |
| `post-tool-use` | `after_tool` | `after_tool` |
| `pre-compact` | `pre_compact` | `pre_compact` |
| `stop` | `stop` | `turn_end` |
| `notification` | `notification` | `notification` |
| `subagent-start` | `subagent_start` | raw only |
| `subagent-stop` | `subagent_stop` | raw only |
| `permission-request` | `permission_request` | raw only |

#### Codex `hooks.json`

| Native Hook | Raw Workflow Event | Semantic Workflow Event |
| --- | --- | --- |
| `SessionStart` | `session_start` | `session_start` |
| `UserPromptSubmit` | `before_agent` | `turn_start` |
| `PreToolUse` | `before_tool` | `before_tool` |
| `PostToolUse` | `after_tool` | `after_tool` |
| `Stop` | `stop` | `turn_end` |

#### Codex App-Server

| Native Event | Raw Workflow Event | Semantic Workflow Event |
| --- | --- | --- |
| `thread/started` | `session_start` | `session_start` |
| `thread/archive` | `session_end` | `session_end` |
| `thread/closed` | `session_end` | `session_end` |
| `turn/started` | `before_agent` | `turn_start` |
| `turn/completed` | `after_agent` | `turn_end` |
| `item/*/requestApproval` | `before_tool` | `before_tool` |
| `item/completed` | `after_tool` | `after_tool` |

#### Gemini

| Native Hook | Raw Workflow Event | Semantic Workflow Event |
| --- | --- | --- |
| `SessionStart` | `session_start` | `session_start` |
| `SessionEnd` | `session_end` | `session_end` |
| `BeforeAgent` | `before_agent` | `turn_start` |
| `AfterAgent` | `after_agent` | `turn_end` |
| `BeforeTool` | `before_tool` | `before_tool` |
| `AfterTool` | `after_tool` | `after_tool` |
| `BeforeToolSelection` | `before_tool_selection` | raw only |
| `BeforeModel` | `before_model` | raw only |
| `AfterModel` | `after_model` | raw only |
| `PreCompress` | `pre_compact` | `pre_compact` |
| `Notification` | `notification` | `notification` |

## Engine And Schema Changes

### Workflow Schema

- Add `TURN_START = "turn_start"` to `RuleTriggerEvent` in
  `src/gobby/workflows/definitions.py`.
- Keep the existing raw workflow trigger values:
  `before_agent`, `after_agent`, `stop`, `before_tool_selection`,
  `before_model`, `after_model`, `subagent_start`, `subagent_stop`,
  `permission_request`.
- Do not add Claude-native names such as `pre_tool_use` or `post_tool_use` to
  the workflow trigger enum.

### Rule Engine

- In `src/gobby/workflows/engine/core.py`, add a `_TURN_START_EVENT_VALUES`
  set parallel to `_TURN_END_EVENT_VALUES`.
- Update `_resolve_rule_events()` so an incoming `HookEventType.BEFORE_AGENT`
  resolves to both:
  - `RuleTriggerEvent.BEFORE_AGENT`
  - `RuleTriggerEvent.TURN_START`
- Keep existing `turn_end` resolution from `HookEventType.AFTER_AGENT` and
  `HookEventType.STOP`.
- Move hardcoded stop-cycle reset logic onto the semantic start boundary:
  - `consecutive_tool_blocks`
  - `_last_blocked_tool`
  - `tool_block_pending`
  - `stop_attempts`
- Keep hardcoded stop-gate increment logic on `turn_end`.
- Keep tool failure / catastrophic failure logic tied to the same semantic
  `turn_end` boundary that drives stop-gate rules today.

### Workflow Hook Bridge

- In `src/gobby/workflows/hooks.py`, add `_is_turn_start_event(...)` parallel
  to `_is_turn_end_event(...)`.
- Use semantic start/end helpers where the bridge currently reasons directly in
  terms of `HookEventType.BEFORE_AGENT` and `HookEventType.STOP`.
- Preserve raw `STOP` special handling only where the response must remain
  stop-specific for safety.
- Keep adapter output unchanged. Adapters continue emitting `HookEventType`
  values such as `BEFORE_AGENT`, `AFTER_AGENT`, and `STOP`; semantic expansion
  happens in the workflow layer.

### Agent Workflow Schema Decision

- Do not add a new generic executable agent event-trigger syntax in v2.
- Keep executable agent workflow primitives limited to what already exists:
  `steps`, `transitions`, `on_enter`, `on_exit`, `on_mcp_success`,
  `on_mcp_error`, and `exit_condition`.
- Keep lifecycle event enforcement in shared rules instead of creating a second
  per-agent event policy surface.

## Bundled Rule Inventory And Changes

Current bundled rule inventory:

| Event | Rule Count |
| --- | --- |
| `before_tool` | 73 |
| `after_tool` | 12 |
| `before_agent` | 8 |
| `session_start` | 10 |
| `turn_end` | 6 |
| `pre_compact` | 1 |

### Convert `before_agent` Rules To `turn_start`

These 8 rules are semantic start-of-turn behavior and should stop depending on
the raw `before_agent` name:

- `src/gobby/install/shared/workflows/rules/task-enforcement/reset-subagent-flag.yaml`
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/memory-capture-nudge.yaml`
- `src/gobby/install/shared/workflows/rules/memory-lifecycle/memory-recall-on-prompt.yaml`
- `src/gobby/install/shared/workflows/rules/auto-task/inject-autonomous-mode.yaml`
- `src/gobby/install/shared/workflows/rules/plan-mode/handle-plan-mode-entry.yaml`
- `src/gobby/install/shared/workflows/rules/messaging/deliver-pending-messages.yaml`
- `src/gobby/install/shared/workflows/rules/messaging/activate-pending-command.yaml`
- `src/gobby/install/shared/workflows/rules/context-handoff/prepare-clear-handoff.yaml`

### Keep Existing `turn_end` Rules

These are already authored against the correct semantic event and should remain
on `turn_end`:

- `src/gobby/install/shared/workflows/rules/memory-lifecycle/digest-on-response.yaml`
- `src/gobby/install/shared/workflows/rules/auto-task/notify-task-tree-complete.yaml`
- `src/gobby/install/shared/workflows/rules/auto-task/guide-task-continuation.yaml`
- `src/gobby/install/shared/workflows/rules/stop-gates/require-step-completion.yaml`
- `src/gobby/install/shared/workflows/rules/stop-gates/require-epic-tree-close.yaml`
- `src/gobby/install/shared/workflows/rules/stop-gates/require-task-close.yaml`

### Keep Stable Tool And Session Rules

No event-name migration is needed for:

- all 73 `before_tool` rules
- 11 standard `after_tool` rules
- all 10 `session_start` rules
- `src/gobby/install/shared/workflows/rules/context-handoff/preserve-context-on-compact.yaml`

### Normalize The Legacy Rule Outlier

- `src/gobby/install/shared/workflows/rules/tool-hygiene/block-escaped-quotes.yaml`
  should be converted to the standard grouped rule schema and changed from a
  direct `event.event_type == 'pre-tool-use'` check to `event: before_tool`.

## Bundled Agent Workflow Changes

There are 13 bundled agent definitions under
`src/gobby/install/shared/workflows/agents/`. All 13 are in scope.

### Global Agent Workflow Changes

Apply these changes across all bundled agent YAMLs:

- Add a short `Lifecycle Model` note in each `instructions:` block:
  - rules are authored against semantic events such as `turn_start` and
    `turn_end`
  - raw `stop`, `before_agent`, and `after_agent` are provider/runtime details
  - end-of-turn gates may keep the current turn alive until required task or
    review work is complete
  - agent termination is separate and still requires `kill_agent`
- Replace ambiguous uses of the word "stop" when they actually mean:
  - end the current turn
  - yield control
  - terminate the agent
- Leave executable lifecycle behavior in shared rules and step transitions;
  agent YAML changes are contract and prompt corrections, not new event logic.

### Per-Agent Corrections

#### `default.yaml`

- Add the standard `Lifecycle Model` note.
- Explicitly teach that `turn_end` is the cross-CLI stop-gate boundary.
- Clarify that raw provider hooks are transport details, not the main authoring
  API.

#### `default-web-chat.yaml`

- Add the same `Lifecycle Model` note as `default.yaml`.
- Clarify that semantic workflow events still apply even though the UI is web
  chat rather than a terminal CLI.

#### `developer.yaml`

- Add the standard `Lifecycle Model` note.
- Replace `Do NOT stop or exit without calling kill_agent.` with wording that
  distinguishes turn-end policy from process termination.
- Clarify that review submission must happen before the session can cleanly end
  a turn under end-of-turn gates.

#### `python-dev.yaml`

- Add the standard `Lifecycle Model` note.
- Make the same `kill_agent` / turn-end distinction as `developer.yaml`.
- Clarify that `mark_task_needs_review` satisfies the workflow before
  termination; raw stop hooks are not the contract.

#### `qa-dev.yaml`

- Add the standard `Lifecycle Model` note.
- Replace `Last stop before merge` with `last review gate before merge`.
- Replace `Do NOT stop or exit without calling kill_agent.` with explicit
  termination wording.
- Keep the "do not reopen" lifecycle guidance, but describe it as task-state
  policy rather than stop-hook behavior.

#### `qa-reviewer.yaml`

- Add the standard `Lifecycle Model` note.
- Replace `Do NOT stop or exit without calling kill_agent.` with explicit
  termination wording.
- Clarify that approval or escalation must happen before the workflow may end
  the turn and terminate.

#### `nightly-linter.yaml`

- Add the standard `Lifecycle Model` note.
- Replace `Do NOT stop without calling kill_agent.` with explicit termination
  wording.
- Clarify that `close_task` must happen before the workflow can satisfy
  end-of-turn gates and move to termination.

#### `nightly-test-fixer.yaml`

- Add the standard `Lifecycle Model` note.
- Replace `Do NOT stop without calling kill_agent.` with explicit termination
  wording.
- Clarify that `close_task` is the step-completion requirement, not a raw stop
  hook side effect.

#### `merge.yaml`

- Add the standard `Lifecycle Model` note.
- Replace `Do NOT stop or exit without calling kill_agent.` with explicit
  termination wording.
- Clarify that merge completion and `kill_agent` are workflow termination
  actions, not raw stop-hook behavior.

#### `expander.yaml`

- Add the standard `Lifecycle Model` note.
- Clarify that `TERMINATE` means explicit agent shutdown after saving the run,
  not a provider-native stop boundary.

#### `expansion-qa.yaml`

- Add the standard `Lifecycle Model` note.
- Clarify that the terminate step is explicit shutdown after saving QA results,
  not stop-hook completion.

#### `pipeline-worker.yaml`

- Add the standard `Lifecycle Model` note.
- Clarify that pipeline completion and any follow-up `send_message` happen
  within the current turn; raw stop hooks are irrelevant to the worker
  contract.

#### `conductor.yaml`

- Add the standard `Lifecycle Model` note.
- Replace `If nothing needs attention, say "No action needed" and stop` with
  wording that means "end the turn" rather than "native stop hook".

## Documentation Changes

### Primary Rule Author Docs

- `docs/guides/rules.md`
  - add `turn_start`
  - present semantic events first
  - move raw events into a separate escape-hatch section
- `docs/guides/workflow-rules.md`
  - add `turn_start` guidance
  - describe reset logic in terms of `turn_start`
  - keep `turn_end` guidance for stop gates
- `docs/guides/workflows-overview.md`
  - describe rules as semantic-first authoring

### Native Hook Reference Docs

- `docs/guides/hook-schemas.md`
  - reframe as native hook transport/reference documentation
  - show native hook -> raw workflow event -> semantic workflow event mapping
  - stop presenting native hook names as the primary rule-author vocabulary
- `docs/guides/mcp-tools.md`
  - clarify `call_hook` takes native hook names, not workflow semantic event
    names

### Variable And Lifecycle Docs

- `docs/guides/variables.md`
  - change `stop_attempts` wording from raw `stop`/`before_agent` language to
    semantic `turn_end` / `turn_start`
  - correct any stale statements about resetting `errors_resolved`
- `docs/references/session-lifecycle.yaml`
  - convert semantic sections from `on_before_agent` to `on_turn_start`
  - convert semantic end-of-turn policy from `on_stop` to `on_turn_end`
  - keep `on_stop` only for truly stop-specific behavior, if any remains
- `src/gobby/install/shared/workflows/rules/CLAUDE.md`
  - update examples to use semantic workflow event names for authoring

## Test Plan

### Engine And Mapping Tests

- Add tests for `RuleTriggerEvent.TURN_START`.
- Add tests that `HookEventType.BEFORE_AGENT` resolves to both:
  - `before_agent`
  - `turn_start`
- Keep and extend tests that `HookEventType.AFTER_AGENT` and `HookEventType.STOP`
  resolve to `turn_end`.
- Update hardcoded rule-engine tests so stop-cycle reset behavior is asserted at
  semantic `turn_start`, not only by mentioning raw `before_agent`.

### Rule Tests

- Update the 8 migrated bundled-rule tests so `body.event.value == "turn_start"`.
- Keep all `turn_end` stop-gate tests and extend them where needed.
- Add a regression test for the normalized `block-escaped-quotes` rule in the
  standard grouped-rule schema.

### Agent Workflow Tests

- Add or update validation tests that all bundled agent YAML files still load
  into `AgentDefinitionBody`.
- Add a bundled-agent content test that asserts:
  - the standard `Lifecycle Model` note is present in all 13 agent files
  - ambiguous phrases such as `last stop before merge` and
    `say "No action needed" and stop` are removed or rewritten
- Keep step-enforcement tests unchanged except where status-message expectations
  need wording updates.

### Documentation Consistency Checks

- Update docs assertions or snapshot-style tests that mention raw
  `before_agent` / `stop` as the primary rule-author model.
- Add a grep-based validation step for the repo migration:
  - no bundled rule should use `event: before_agent`
  - no bundled rule should directly match `pre-tool-use`
  - no bundled agent should teach raw `stop` as the universal lifecycle
    boundary

## Acceptance Criteria

- Rule authors can write cross-CLI "do not let the agent yield yet" rules using
  `turn_end` only.
- Rule authors can write cross-CLI prompt/start-of-turn rules using
  `turn_start` only.
- All 8 semantic start-of-turn bundled rules are migrated from `before_agent`
  to `turn_start`.
- No bundled rule directly checks Claude kebab-case hook names.
- All 13 bundled agent files are updated to use the semantic lifecycle
  vocabulary consistently.
- The workflow engine docs, variable docs, and hook docs all agree on the
  semantic/raw split.

## Non-Goals

- No adapter wire format change.
- No installer change to provider-native hook names.
- No rename of existing hook handler methods such as `handle_before_agent`.
- No new generic executable event-trigger syntax in agent YAML in v2.
