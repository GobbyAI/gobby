# Plan: Claude Code hook parity v2

## Goal

Bring Gobby's Claude integration to parity with Anthropic's current hook
contract so every documented Claude hook can be:

- installed and uninstalled
- translated into `HookEvent`
- routed through handlers, workflows, and websocket broadcast
- translated back into a docs-compliant Claude response

## Current baseline in this repo

- `ClaudeCodeAdapter` recognizes 12 native Claude hook types.
- `src/gobby/install/claude/hooks-template.json` and
  `src/gobby/cli/installers/claude.py` already install/clean up 11 PascalCase
  hooks: `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`,
  `PostToolUse`, `PreCompact`, `Notification`, `Stop`, `SubagentStart`,
  `SubagentStop`, `PermissionRequest`.
- `src/gobby/install/shared/hooks/validate_settings.py` still validates only 10
  Claude hooks and already omits `PermissionRequest`.
- `post-tool-use-failure` is only partially wired:
  - it maps to `AFTER_TOOL`
  - both `ClaudeCodeAdapter.HOOK_EVENT_NAME_MAP` and the HTTP graceful-error
    path wrongly emit `PostToolUse`
- `ClaudeCodeAdapter.translate_from_hook_response()` only understands:
  - generic top-level `continue` / `decision` / `stopReason` / `systemMessage`
  - `additionalContext` on 4 events
  - PreToolUse-specific `updatedInput` / `permissionDecision`

This is not just "add 14 missing events." The current Claude response contract
is already wrong for some events that Gobby claims to support today.

## Contract facts from Claude docs

Source of truth: https://code.claude.com/docs/en/hooks

- Claude currently documents 26 hook events.
- `additionalContext` is valid on exactly 7 events:
  - `SessionStart`
  - `UserPromptSubmit`
  - `PreToolUse`
  - `PostToolUse`
  - `PostToolUseFailure`
  - `Notification`
  - `SubagentStart`
- `Stop` and `SubagentStop` use top-level `decision` + `reason`.
- `StopFailure`, `TaskCreated`, `TaskCompleted`, and `TeammateIdle` are
  hard-stop style responses: `continue: false` with `stopReason`.
- `PermissionRequest` is special: decision lives under
  `hookSpecificOutput.decision`, not the generic top-level `decision`.
- `ConfigChange` supports blocking, so do not suppress top-level decision fields
  for it.
- `continue: false` is a universal hard stop. Do not emit contradictory
  allow-style output alongside it.

Do not encode this policy from memory in multiple places. Put the docs-derived
rules in one shared table and reuse it everywhere.

## Missing and partial events

Missing from install-time registration and internal wiring today:

- `PermissionDenied`
- `PostCompact`
- `StopFailure`
- `TaskCreated`
- `TaskCompleted`
- `TeammateIdle`
- `InstructionsLoaded`
- `ConfigChange`
- `CwdChanged`
- `FileChanged`
- `WorktreeCreate`
- `WorktreeRemove`
- `Elicitation`
- `ElicitationResult`

Partially implemented and still wrong:

- `PostToolUseFailure`
- `PermissionRequest`
- `Notification`
- `SubagentStart`

## Implementation strategy

### Phase 0 — Centralize Claude contract data

Create a shared module, e.g. `src/gobby/adapters/claude_contract.py`, as the
single source of truth for:

- native Claude hook names
- PascalCase `hookEventName` values
- the 7-event `additionalContext` allowlist
- per-event response-shape policy:
  - top-level `decision`
  - top-level `reason`
  - top-level `stopReason`
  - nested `hookSpecificOutput.decision`
  - event-specific output fields
- helper utilities for graceful-error responses

Why first:

- `src/gobby/adapters/claude_code.py` and
  `src/gobby/servers/routes/mcp/hooks.py` already drifted apart.
- A second round of duplicated maps guarantees more skew.

Implementation note:

- The installer currently writes
  `ghook --gobby-owned --cli=claude --type=...`, not
  `$HOOKS_DIR/hook_dispatcher.py`. Keep the plan aligned with the real install
  path.

### Phase 1 — Fix existing parity bugs before adding new events

Files:

- `src/gobby/adapters/claude_code.py`
- `src/gobby/servers/routes/mcp/hooks.py`
- `tests/adapters/test_claude_code_adapter.py`

1. Fix `post-tool-use-failure` so both normal responses and graceful-error
   responses emit `PostToolUseFailure`.
2. Expand `additionalContext` support from 4 events to the docs-backed 7
   events.
3. Stop emitting blanket top-level `decision: approve|block` for every Claude
   response. Shape fields per event using the shared contract table.
4. Rework `_graceful_error_response()` to use the same helper tables as the
   adapter, including `SessionStart`.
5. Add a dedicated `PermissionRequest` translation path. Current
   PreToolUse-specific logic is not enough for this event.

Tests:

- Add context-injection coverage for `Notification`, `SubagentStart`, and
  `PostToolUseFailure`.
- Keep `Stop` asserting no `additionalContext`.
- Add explicit `PermissionRequest` response-shape coverage once the policy
  table lands.
- Update the current notification round-trip test: "no `hookSpecificOutput`"
  becomes wrong once `additionalContext` is allowed.

### Phase 2 — Add the missing Claude events to Gobby's core model

Files:

- `src/gobby/hooks/events.py`
- `src/gobby/hooks/hook_types.py`
- `src/gobby/hooks/event_handlers/__init__.py`
- `src/gobby/hooks/broadcaster.py`
- `src/gobby/workflows/definitions.py`
- `src/gobby/workflows/engine/core.py`

Add new unified events and keep every required map in lockstep:

- `PERMISSION_DENIED`
- `POST_COMPACT`
- `STOP_FAILURE`
- `TASK_CREATED`
- `TASK_COMPLETED`
- `TEAMMATE_IDLE`
- `INSTRUCTIONS_LOADED`
- `CONFIG_CHANGE`
- `CWD_CHANGED`
- `FILE_CHANGED`
- `WORKTREE_CREATE`
- `WORKTREE_REMOVE`
- `ELICITATION`
- `ELICITATION_RESULT`

Required surfaces:

- `HookEventType`
- `EVENT_TYPE_CLI_SUPPORT`
- `HookType`
- `HOOK_INPUT_MODELS`
- `HOOK_OUTPUT_MODELS`
- `EventHandlers._handler_map`
- `EVENT_TYPE_TO_HOOK_TYPE`
- `RuleTriggerEvent`
- `_TURN_END_EVENT_VALUES`

Important semantic fix:

- Add `STOP_FAILURE` to `_TURN_END_EVENT_VALUES`; that set, not
  `_resolve_rule_events()`, controls the `turn_end` alias.

Handler stance:

- Default Tier 3 events to observe-only handlers returning an empty
  `HookResponse()` unless the docs require real behavior immediately.

### Phase 3 — Wire missing Claude names through adapter and installer

Files:

- `src/gobby/adapters/claude_code.py`
- `src/gobby/install/claude/hooks-template.json`
- `src/gobby/cli/installers/claude.py`
- `src/gobby/install/shared/hooks/validate_settings.py`

1. Extend `ClaudeCodeAdapter.EVENT_MAP` and `HOOK_EVENT_NAME_MAP` for the 14
   missing native events.
2. Add the 15 missing install/uninstall entries:
   - `PostToolUseFailure`
   - `PermissionDenied`
   - `PostCompact`
   - `StopFailure`
   - `TaskCreated`
   - `TaskCompleted`
   - `TeammateIdle`
   - `InstructionsLoaded`
   - `ConfigChange`
   - `CwdChanged`
   - `FileChanged`
   - `WorktreeCreate`
   - `WorktreeRemove`
   - `Elicitation`
   - `ElicitationResult`
3. Update Claude validation to require the full installed set of 26 hooks.
4. Fix the existing validation gap for `PermissionRequest`; it is already
   installed but is not currently required by `validate_settings.py`.

Before merging:

- Verify the exact `--type=` values Claude emits and accepts. The repo
  currently infers kebab-case names; confirm them against the official docs
  and a scratch install if needed.

### Phase 4 — Add event-specific output support end to end

This phase is where "event coverage" becomes actual parity. New
`HookResponse` fields are useless unless rules can produce them and the adapter
can serialize them.

At minimum, cover these docs-backed outputs:

| Event | Output fields | Suggested `HookResponse` fields | Rule surface |
|---|---|---|---|
| `PermissionRequest` | `hookSpecificOutput.decision`, `updatedInput`, `updatedPermissions` | `permission_decision`, `modified_input`, `updated_permissions` | extend existing rewrite flow or add a dedicated permission effect |
| `PermissionDenied` | `retry` | `retry` | `set_retry` |
| `CwdChanged` | `watchPaths` | `watch_paths` | `set_watch_paths` |
| `FileChanged` | `watchPaths` | `watch_paths` | `set_watch_paths` |
| `WorktreeCreate` | `worktreePath` | `worktree_path` | `set_worktree_path` |
| `Elicitation` | `action`, `content`, `errorMessage` | `elicitation_action`, `elicitation_content`, `elicitation_error` | `set_elicitation` |
| `ElicitationResult` | `action` | `elicitation_action` | `set_elicitation` |

Implementation chain:

1. Add the new optional fields to `HookResponse`.
2. Extend `HookOutput` subclasses so websocket broadcasts can carry the same
   data.
3. Extend `RuleEffect` with the new effect types and payload fields.
4. Extend `EffectsMixin._apply_effect()` to stash the values in `variables`.
5. Extend `RuleEngine.evaluate()` to lift those values into `HookResponse` in
   every response-building branch.
6. Extend `ClaudeCodeAdapter.translate_from_hook_response()` to serialize the
   fields into the correct Claude output shape.

Do not skip the middle layers:

- Adding adapter branches without new `RuleEffect` types means rule authors
  still cannot produce the outputs.
- Adding `HookResponse` fields without `HookOutput` model changes means
  broadcaster subscribers never see them.

### Phase 5 — Keep observe-only events intentionally observe-only

Document the default behavior for newly added Tier 3 events so future readers
do not assume missing reconciliation is a bug:

- `TaskCreated` / `TaskCompleted`: observe only; no automatic `gobby-tasks`
  mutation.
- `WorktreeCreate` / `WorktreeRemove`: observe only; no auto-registry update.
- `Elicitation` / `ElicitationResult`: event pair is surfaced; no automatic
  reply generation.
- `CwdChanged`, `FileChanged`, `InstructionsLoaded`, `ConfigChange`,
  `TeammateIdle`: surfaced for rules and telemetry; no downstream state sync.

If future behavior is desired, file separate follow-up tasks rather than
bloating this parity pass.

## Critical files

- `src/gobby/adapters/claude_contract.py` (new): shared Claude response policy
  and event-name tables
- `src/gobby/adapters/claude_code.py`: adapter event maps and response
  translation
- `src/gobby/servers/routes/mcp/hooks.py`: graceful-error response must reuse
  the same contract
- `src/gobby/hooks/events.py`: unified enum and `HookResponse`
- `src/gobby/hooks/hook_types.py`: Claude `HookType` enum plus input/output
  model maps
- `src/gobby/hooks/event_handlers/__init__.py`: handler registration coverage
- `src/gobby/hooks/broadcaster.py`: unified event type to `HookType` mapping
- `src/gobby/workflows/definitions.py`: `RuleTriggerEvent` and `RuleEffect`
- `src/gobby/workflows/engine/effects.py`: new effect application branches
- `src/gobby/workflows/engine/core.py`: `turn_end` aliasing and `HookResponse`
  construction
- `src/gobby/install/claude/hooks-template.json`: installed Claude hook
  registration
- `src/gobby/cli/installers/claude.py`: uninstall cleanup list
- `src/gobby/install/shared/hooks/validate_settings.py`: parity validation
- `tests/adapters/test_claude_code_adapter.py`: adapter contract coverage
- `tests/hooks/test_event_handlers.py`: handler map coverage
- `tests/hooks/test_events.py` and `tests/hooks/test_hooks_events.py`: enum
  parity and CLI support matrix
- `tests/hooks/test_hook_types.py`: `HookType` and model-map coverage
- `tests/hooks/test_broadcaster.py`: broadcaster mapping coverage
- `tests/workflows/test_rule_models.py`: `RuleTriggerEvent` and `RuleEffect`
  coverage
- `tests/workflows/test_rule_engine.py`: `turn_end` coverage and end-to-end
  effect tests

## Recommended execution order

1. Add the shared Claude contract module and move existing adapter/error-path
   logic onto it.
2. Fix current parity bugs:
   - `PostToolUseFailure`
   - 7-event `additionalContext`
   - `PermissionRequest`
   - graceful-error skew
3. Add the 14 missing unified events and keep all enum/model/handler/broadcaster
   maps exhaustive.
4. Extend installer, uninstall cleanup, and validation to the full 26-hook
   Claude set.
5. Add the new event-specific rule effects and adapter serialization.
6. Mark observe-only handlers explicitly and leave deeper downstream
   reconciliation for follow-up tasks.

## Verification

1. `uv run pytest tests/adapters/test_claude_code_adapter.py tests/hooks/ tests/workflows/ -v`
2. `uv run ruff check src/gobby/adapters/ src/gobby/hooks/ src/gobby/workflows/ src/gobby/cli/installers/ src/gobby/install/ src/gobby/servers/`
3. `uv run mypy src/gobby/adapters/ src/gobby/hooks/ src/gobby/workflows/ src/gobby/servers/`
4. Fresh scratch install/uninstall:
   - verify all 26 Claude hook keys appear in `.claude/settings.json`
   - verify uninstall removes the same 26 keys
5. `gobby doctor` or the shared validator reports full Claude parity, including
   `PermissionRequest`
6. Manual checks:
   - `notification` + `inject_context` reaches Claude via `additionalContext`
   - `post-tool-use-failure` emits `PostToolUseFailure` in both normal and
     graceful-error paths
   - `PermissionRequest` emits nested `hookSpecificOutput.decision`, not
     top-level `decision`
   - `StopFailure` fires `turn_end` rules and omits fields not allowed by the
     docs-derived policy
   - new Tier 3 events show up on websocket subscribers instead of being
     dropped

No full suite run is needed for this plan update. The targeted hook, workflow,
and installer slices are enough.
