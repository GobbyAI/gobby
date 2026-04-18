# Plan: Full Claude Code hook event coverage (post-review)

## Context

Claude Code's hook reference enumerates **26** named hook events
(https://code.claude.com/docs/en/hooks). Gobby's adapter at
`src/gobby/adapters/claude_code.py` handles 12 and emits
`hookSpecificOutput.additionalContext` on only 4 — so rules firing
`inject_context` on `SubagentStart`, `Notification`, and `PostToolUseFailure`
are silently routed to `systemMessage` (user-only). 14 documented events are
not wired at all.

The user wants full parity so rules can bind to any documented event. A prior
plan revision under-scoped the work; this revision incorporates Codex's review
findings.

Rule audit (112 rules) found **no mis-bound rules** — the problem is adapter
under-emission, missing install-time registration, handler-map gaps,
event-specific output schemas, and missing model/broadcaster coverage.

## Scope corrections from review (both rounds)

- **`Stop` does NOT support `additionalContext`.** Docs show only `decision`
  and `reason` for Stop/SubagentStop. Stop is removed from the
  `valid_hook_event_names` expansion.
- **`continue: false` is a universal hard stop** that overrides event-specific
  decision fields. Audit adapter for accidental `continue: false` +
  `decision: approve` pairings.
- **Event count is 26**, not 28. Test names and enumerations must reflect that.
- **`additionalContext` is supported on exactly 7 events**: `SessionStart`,
  `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `PostToolUseFailure`,
  `Notification`, `SubagentStart`.
- **`StopFailure`** is turn-ending per docs; alias into the `turn_end`
  semantic event alongside `stop` and `after_agent`.
- **Per-event top-level field suppression.** Adapter unconditionally emits
  `decision` (lines 189–195). Docs say several events (`StopFailure`,
  `SessionStart`, `SessionEnd`, pure observability events) don't accept it.
  Adapter needs a per-event allowlist for which top-level fields are emitted.
- **Rule-effect surface cannot be bypassed.** Adding new `HookResponse` fields
  without new `RuleEffect` types + a corresponding lift in
  `RuleEngine.evaluate()` means rules literally cannot produce those outputs.
  See Phase 5 revision.
- **HTTP graceful-error path has its own map.**
  `src/gobby/servers/routes/mcp/hooks.py:33` carries a duplicate
  `HOOK_EVENT_NAME_MAP` with the same `post-tool-use-failure → PostToolUse`
  bug, independent of the adapter.

## Work breakdown

### Phase 1 — Adapter correctness fixes

**File:** `src/gobby/adapters/claude_code.py`

1. Line 234 — expand `valid_hook_event_names` to **seven** (not eight):
   add `SubagentStart`, `Notification`, `PostToolUseFailure` to the existing
   four. Stop stays out.
2. Line 145 — fix `HOOK_EVENT_NAME_MAP[post-tool-use-failure]`: currently
   emits `PostToolUse`, must emit `PostToolUseFailure`.
3. Audit `translate_from_hook_response` (lines 178–196) for any path that sets
   `continue: false` alongside `decision: approve` — should be impossible
   given current logic but confirm.
4. **Per-event top-level field allowlist** (new). Add a class-level map
   keyed by `hookEventName`: the set of top-level fields the adapter is
   permitted to emit for that event. Default allows universal fields
   (`continue`, `systemMessage`, `stopReason`). Events like `StopFailure`,
   `SessionStart`, `SessionEnd`, `FileChanged`, `CwdChanged`,
   `InstructionsLoaded`, `ConfigChange`, `TeammateIdle` do NOT get `decision`
   or `reason` per docs — strip those for those events before returning.
   Confirms docs-compliant output shape event-by-event.
5. Update module-level docstring (lines 7–15) to list the updated event set.

**Tests:** `tests/adapters/test_claude_code_adapter.py`

- Add end-to-end `additionalContext` assertions for `SubagentStart`,
  `Notification`, `PostToolUseFailure` (follow `context_injection_*` pattern
  around line 510).
- Update line 790 (Notification) assertion that currently expects no
  `hookSpecificOutput` — behavior intentionally changing.
- Update line 96 expectation for `post-tool-use-failure` →
  `PostToolUseFailure`.
- Leave line 564 (Stop = no hookSpecificOutput) intact — Stop isn't getting
  `additionalContext`, so that assertion remains correct.

### Phase 2 — Install-time registration + duplicate error-path map (NEW from review)

**Files:**
- `src/gobby/install/claude/hooks-template.json`
- `src/gobby/cli/installers/claude.py` (`_GOBBY_HOOK_TYPES`, line 31)
- `src/gobby/install/shared/hooks/validate_settings.py` (line 49)
- `src/gobby/servers/routes/mcp/hooks.py` (line 33: duplicate `HOOK_EVENT_NAME_MAP`)

1. Add a top-level entry to `hooks-template.json` for each of the 14 new
   events + `PostToolUseFailure` (which is currently not in the template at
   all — it only comes in as a variant of `post-tool-use`). Pattern matches
   existing entries: a `hooks` array containing a `type: command` entry that
   invokes `$HOOKS_DIR/hook_dispatcher.py` with `--cli=claude` and
   `--type=<kebab-case-name>`. No matcher unless docs specify one.

2. Extend `_GOBBY_HOOK_TYPES` in `claude.py:31` with all 15 additions
   (`PostToolUseFailure`, `PermissionDenied`, `PostCompact`, `StopFailure`,
   `TaskCreated`, `TaskCompleted`, `TeammateIdle`, `InstructionsLoaded`,
   `ConfigChange`, `CwdChanged`, `FileChanged`, `WorktreeCreate`,
   `WorktreeRemove`, `Elicitation`, `ElicitationResult`). This list drives
   uninstall cleanup — incomplete list = orphan hooks on uninstall.

3. Update `validate_settings.py:49` to include the new events in parity
   validation so `gobby doctor` / health checks catch missing registrations.

4. **Fix graceful-error path end-to-end** at `servers/routes/mcp/hooks.py`:33
   (duplicate `HOOK_EVENT_NAME_MAP`) and :50 (`_graceful_error_response`).
   The existing function unconditionally returns `decision: "approve"`, which
   conflicts with the new adapter rule that suppresses `decision` for
   observability events like `StopFailure`/`FileChanged`/`CwdChanged` etc.
   Preferred fix: **share the adapter's allowlist helpers** — extract the
   7-event `additionalContext` allowlist AND the per-event top-level field
   suppression map into a shared module (e.g.,
   `src/gobby/adapters/claude_contract.py` or a class-level static on
   `ClaudeCodeAdapter`) imported by both the adapter and the graceful-error
   response. Update the map to:
   - Include all 7 additionalContext-capable events: `pre-tool-use`,
     `user-prompt-submit`, `post-tool-use`, `post-tool-use-failure`,
     `session-start`, `subagent-start`, `notification` (SessionStart is
     currently missing from this map, which is a latent bug).
   - Emit `decision: "approve"` only when the target event accepts a
     top-level `decision` field per docs; strip it otherwise.
   `_graceful_error_response()` then produces docs-compliant output for
   every event, matching the adapter's contract.

5. **Assumption check (blocker before coding):** kebab-case native names are
   inferred. As step 1 of implementation, verify exact `--type=` values
   Claude Code's `hook_dispatcher.py` accepts (source is external to this
   repo — check via manual hook trigger or by inspecting what Claude Code
   sends). Correct any mismatches before merging.

### Phase 3 — Core plumbing (NEW from review)

**Enums and handler map:**

1. **`src/gobby/hooks/events.py`** — two code points per event:
   - Add `HookEventType` enum values: `PERMISSION_DENIED`, `POST_COMPACT`,
     `STOP_FAILURE`, `TASK_CREATED`, `TASK_COMPLETED`, `TEAMMATE_IDLE`,
     `INSTRUCTIONS_LOADED`, `CONFIG_CHANGE`, `CWD_CHANGED`, `FILE_CHANGED`,
     `WORKTREE_CREATE`, `WORKTREE_REMOVE`, `ELICITATION`, `ELICITATION_RESULT`.
   - Update `EVENT_TYPE_CLI_SUPPORT` at line 146 to declare which CLIs
     (claude, gemini, codex) support each new event. For Claude-only events,
     mark other CLIs as unsupported so cross-CLI rule authors get correct
     enforcement.

2. **`src/gobby/hooks/hook_types.py:44`** (`HookType` enum) — mirror with
   kebab-case variants.

3. **`src/gobby/hooks/hook_types.py:545`** (model maps) — register each new
   event's input payload shape.

4. **`src/gobby/hooks/event_handlers/__init__.py:109`** — extend
   `_handler_map` with a handler method per new event. For events without
   concrete business logic yet, add `handle_<event>` that returns an empty
   `HookResponse` (observe-only). Failing to add entries makes `get_handler`
   return `None` and silently drops events.

5. **Broadcaster triple-map update** — the broadcaster silently drops events
   at `broadcaster.py:96` if any of three maps lacks an entry. All three
   need updates for every new event:
   - `EVENT_TYPE_TO_HOOK_TYPE` at `src/gobby/hooks/broadcaster.py:25`
     (snake_case event type → `HookType` enum)
   - `HOOK_INPUT_MODELS` at `src/gobby/hooks/hook_types.py:545`
     (input payload Pydantic model per event)
   - `HOOK_OUTPUT_MODELS` at `src/gobby/hooks/hook_types.py:561`
     (output payload Pydantic model per event)
   For observe-only events without bespoke fields, input/output models can
   extend the existing `HookInput`/`HookOutput` base classes with minimal
   fields. Verify frontend websocket consumers either handle the new event
   types or explicitly ignore them.

6. **`src/gobby/workflows/definitions.py:57`** (`RuleTriggerEvent`) — add
   snake_case values matching the `HookEventType` additions.

7. **`src/gobby/workflows/engine/core.py`** — two code points:
   - `_resolve_rule_events` (line 93): map each new `HookEventType` to its
     `RuleTriggerEvent`.
   - `_TURN_END_EVENT_VALUES` (line 44): add `STOP_FAILURE` to this set so
     it aliases into `turn_end` alongside `after_agent` and `stop`. This
     constant — not `_resolve_rule_events` — is what actually controls the
     semantic alias.

**Tests requiring enum/coverage updates** (expanded from Codex review):
- `tests/hooks/test_event_handlers.py:79` — `_handler_map` exhaustive coverage
- `tests/hooks/test_events.py:84` and `:118` — enum completeness
- `tests/hooks/test_hooks_events.py:50` and `:128` — enum parity
- `tests/hooks/test_hook_types.py:56` — `HookType` enum + model map coverage
- `tests/hooks/test_broadcaster.py:159` — broadcaster triple-map coverage
- `tests/workflows/test_rule_models.py` — `RuleTriggerEvent` + `RuleEffect.type` updates
- `tests/workflows/test_rule_engine.py:2332` — semantic `turn_end` coverage
  (must now include `STOP_FAILURE`)

### Phase 4 — Adapter event wiring

**File:** `src/gobby/adapters/claude_code.py`

For each of the 14 new events:
1. Add kebab-case → `HookEventType` entry to `EVENT_MAP` (line 46).
2. Add kebab-case → PascalCase entry to `HOOK_EVENT_NAME_MAP` (line 138).

Module docstring (lines 7–15) updates to list all 26 events.

### Phase 5 — Event-specific output schemas end-to-end (REWRITTEN from review)

`translate_from_hook_response` currently handles:
- Universal `continue`, `decision`, `stopReason`, `systemMessage`
- `hookSpecificOutput.additionalContext` for the 4 (soon 7) allowlist events
- `hookSpecificOutput.updatedInput` / `permissionDecision` for PreToolUse
  (via `response.modified_input` + `response.auto_approve`)

The pattern for PreToolUse is the template: new `HookResponse` fields are
populated by a dedicated `RuleEffect` type, lifted into `HookResponse` at
`RuleEngine.evaluate()`, and translated to output fields in the adapter.

**Event-specific output fields to add:**

| Event | Output field | `HookResponse` field | New `RuleEffect` type |
|---|---|---|---|
| `PermissionDenied` | `retry` (bool) | `retry: bool \| None` | `set_retry` |
| `CwdChanged` | `watchPaths` (list[str]) | `watch_paths: list[str] \| None` | `set_watch_paths` |
| `FileChanged` | `watchPaths` (list[str]) | (same field) | (same effect) |
| `WorktreeCreate` | `worktreePath` (str) | `worktree_path: str \| None` | `set_worktree_path` |
| `Elicitation` | `action`, `content`, `errorMessage` | `elicitation_action`, `elicitation_content`, `elicitation_error` | `set_elicitation` |
| `ElicitationResult` | `action` | (reuse `elicitation_action`) | (same effect) |

**Implementation (end-to-end, in order):**

1. **`src/gobby/hooks/events.py`** — add optional fields to `HookResponse`
   for each bespoke output (above). The same fields must flow through to
   `HookOutput` models so websocket broadcasts carry them — update
   `HookOutput` subclasses in `src/gobby/hooks/hook_types.py` for
   `PermissionDeniedOutput`, `CwdChangedOutput`/`FileChangedOutput`,
   `WorktreeCreateOutput`, `ElicitationOutput`/`ElicitationResultOutput`
   to include `retry`/`watchPaths`/`worktreePath`/`action`/`content`/
   `errorMessage` respectively. Response serialization in
   `src/gobby/hooks/broadcaster.py:128` then propagates them to subscribers.
2. **`src/gobby/workflows/definitions.py:85`** (`RuleEffect`) — extend the
   `type` Literal with the 5 new effect names. Add optional fields carrying
   the effect payload (e.g., `retry: bool | None`, `watch_paths: list[str] | None`,
   `worktree_path: str | None`, `elicitation_action: str | None`, etc.).
3. **`src/gobby/workflows/engine/effects.py:39`** (`_apply_effect`) — add a
   branch for each new effect type that writes its payload to `variables`
   under a reserved key (e.g., `variables["_set_retry"] = effect.retry`),
   following the existing `_rewrite_input` convention.
4. **`src/gobby/workflows/engine/core.py:524`** (`RuleEngine.evaluate` response
   build) — extend the response-building branches (all four: override block,
   override allow, block_reason, default allow) to pop the new reserved keys
   from `variables` and pass them into the `HookResponse` constructor. Follow
   the exact pattern used for `_rewrite_input → modified_input, auto_approve`
   at lines 530–535.
5. **`src/gobby/adapters/claude_code.py`** (`translate_from_hook_response`) —
   add event-specific branches that read the new `HookResponse` fields and
   emit them into `hookSpecificOutput` (following the existing PreToolUse
   `updatedInput`/`permissionDecision` branch at lines 244–248). Gate by
   `hookEventName` — e.g., `retry` only on `PermissionDenied`, `watchPaths`
   only on `CwdChanged`/`FileChanged`, etc.
6. **Test the full loop per effect**: rule YAML → effect → variables →
   `HookResponse` → adapter output → final JSON. Add an integration test per
   effect in `tests/workflows/test_rule_engine.py` plus an adapter-level
   translation test.

**Why this matters:** without steps 2–4, the new `HookResponse` fields are
unreachable from rules (no effect type produces them, and `core.py:524`
doesn't know how to lift them). Steps 1 + 5 alone leave the feature
non-functional for rule authors.

### Phase 6 — Downstream integration decisions (observe-only defaults)

For each Tier 3 event, default behavior is observe-only: event flows, rules
can bind, but no automatic cross-subsystem state reconciliation. Record
intent as docstring/comment on each handler.

- `TaskCreated`/`TaskCompleted` — observe-only; no auto-sync to gobby-tasks.
- `WorktreeCreate`/`WorktreeRemove` — observe-only; no auto-registry in
  `src/gobby/worktrees/`.
- `Elicitation`/`ElicitationResult` — wire the event pair; no automatic
  response. Rules can intercept and set `action`/`content`.
- `CwdChanged` — surface to rule engine; project-scoped rules may re-key.
- Everything else (`ConfigChange`, `InstructionsLoaded`, `FileChanged`,
  `TeammateIdle`) — pure observability. No downstream consumers added.

Reconciliation work for tasks/worktrees filed as separate follow-up tasks if
a concrete need emerges.

## Critical files (expanded from review)

| File | Role |
|---|---|
| `src/gobby/adapters/claude_code.py` | Event maps, translation, allowlist, per-event top-level field suppression, event-specific output |
| `src/gobby/hooks/events.py` | `HookEventType` enum + `EVENT_TYPE_CLI_SUPPORT` (line 146) + new `HookResponse` fields |
| `src/gobby/hooks/hook_types.py` | `HookType` enum (line 44), `HOOK_INPUT_MODELS` (line 545), `HOOK_OUTPUT_MODELS` (line 561) |
| `src/gobby/hooks/event_handlers/__init__.py` | `_handler_map` (line 109) + new handler methods |
| `src/gobby/hooks/broadcaster.py` | `EVENT_TYPE_TO_HOOK_TYPE` (line 25), `broadcast_event` (line 63) |
| `src/gobby/workflows/definitions.py` | `RuleTriggerEvent` enum (line 57), `RuleEffect` type literal (line 88) + new payload fields |
| `src/gobby/workflows/engine/effects.py` | `_apply_effect` (line 39) — new effect type branches |
| `src/gobby/workflows/engine/core.py` | `_TURN_END_EVENT_VALUES` (line 44) — add `STOP_FAILURE`; `_resolve_rule_events` (line 93) semantic mapping for other new events; `RuleEngine.evaluate` response build (line 524) — lift new fields from `variables` to `HookResponse` |
| `src/gobby/install/claude/hooks-template.json` | Claude `settings.json` hook registration |
| `src/gobby/cli/installers/claude.py` | `_GOBBY_HOOK_TYPES` (line 31) cleanup list |
| `src/gobby/install/shared/hooks/validate_settings.py` | Parity validation (line 49) |
| `src/gobby/servers/routes/mcp/hooks.py` | `HOOK_EVENT_NAME_MAP` (line 33) + `_graceful_error_response` (line 50) — reuse adapter contract helper; add SessionStart |
| `src/gobby/adapters/claude_contract.py` (new) | Shared module holding the 7-event additionalContext allowlist + per-event top-level field suppression map. Consumed by adapter and graceful-error path. |
| `tests/adapters/test_claude_code_adapter.py` | Adapter tests — update line 96 and line 790; add 14 new event tests + per-event field suppression tests |
| `tests/hooks/test_event_handlers.py` | Handler map coverage (line 79) |
| `tests/hooks/test_events.py` | Enum completeness (lines 84, 118) |
| `tests/hooks/test_hooks_events.py` | Enum parity (lines 50, 128) |
| `tests/hooks/test_hook_types.py` | `HookType` + model maps (line 56) |
| `tests/hooks/test_broadcaster.py` | Broadcaster triple-map coverage (line 159) |
| `tests/workflows/test_rule_models.py` | `RuleTriggerEvent` + `RuleEffect.type` |
| `tests/workflows/test_rule_engine.py` | Semantic `turn_end` coverage (line 2332) + integration tests for new effect types |

## Utilities to reuse

- `compress_and_truncate` (`gobby.llm.sdk_utils`) — `additionalContext` compression
- `build_first_hook_session_metadata_lines` (`gobby.adapters.base`) — session metadata injection
- `normalize_tool_fields` (`gobby.hooks.normalization`) — tool field alias resolution
- Existing `handle_native_*` test pattern in the adapter test file

## Execution order

1. **Verify kebab-case native names** by triggering each new event manually or
   inspecting Claude Code's `hook_dispatcher.py`. Everything downstream
   depends on correct identifiers.
2. **Phase 1** — adapter correctness (3 one-line fixes + per-event top-level
   field allowlist + tests). `uv run pytest tests/adapters/test_claude_code_adapter.py -v`.
3. **Phase 2** — install-time registration + fix duplicate `HOOK_EVENT_NAME_MAP`
   in `servers/routes/mcp/hooks.py`. Verify via fresh
   `gobby install`/`gobby uninstall` cycle into a scratch `.claude/` directory.
4. **Phase 3** — enums (`HookEventType`, `HookType`, `RuleTriggerEvent`),
   handler map, broadcaster triple-map (`EVENT_TYPE_TO_HOOK_TYPE`,
   `HOOK_INPUT_MODELS`, `HOOK_OUTPUT_MODELS`). `uv run pytest tests/hooks/ -v`.
5. **Phase 4** — adapter event wiring (`EVENT_MAP`, `HOOK_EVENT_NAME_MAP`).
   Re-run adapter tests.
6. **Phase 5** — event-specific output schemas end-to-end:
   a. Add `HookResponse` fields.
   b. Add `RuleEffect` type literals + payload fields.
   c. Extend `_apply_effect` with new branches.
   d. Extend `RuleEngine.evaluate` response-build (all 4 branches) to lift
      new variables → `HookResponse`.
   e. Extend adapter `translate_from_hook_response` with per-event output
      branches.
   f. Integration tests per effect (rule YAML → final JSON).
7. **Phase 6** — observability integration comments/docstrings on observe-only
   handlers. Document rationale so future readers know why handlers are
   stubs.

## Verification

1. `uv run pytest tests/adapters/test_claude_code_adapter.py tests/hooks/ tests/workflows/ -v` — all green.
2. `uv run ruff check src/gobby/adapters/ src/gobby/hooks/ src/gobby/workflows/ src/gobby/cli/installers/ src/gobby/install/ src/gobby/servers/` — clean.
3. `uv run mypy src/gobby/adapters/ src/gobby/hooks/ src/gobby/workflows/ src/gobby/servers/` — clean.
4. `gobby install --global` into a scratch `~/.claude/settings.json`, verify all
   26 hook types registered. Then `gobby uninstall` and verify all 26 removed.
5. `gobby doctor` (or equivalent) reports full hook parity.
6. Manual end-to-end:
   - Rule bound to `notification` that calls `inject_context` — confirm the
     injected text reaches the model on the subsequent turn (not as a
     systemMessage to the user).
   - `post-tool-use-failure` event — confirm both the adapter response AND the
     `_graceful_error_response` path emit `hookEventName: PostToolUseFailure`.
   - Throwaway rule bound to a Tier 3 event (e.g., `cwd_changed`) — verify via
     `gobby rules list` it loads and fires on a manual trigger.
   - `PermissionDenied` rule using new `set_retry` effect — confirm `retry: true`
     propagates through effect → variables → `HookResponse` → final JSON output.
   - `WorktreeCreate` rule using new `set_worktree_path` effect — confirm
     `worktreePath` lands in `hookSpecificOutput`.
   - Trigger a `StopFailure` event — confirm the adapter omits `decision`/`reason`
     from the response (per-event field suppression working) and that rules
     bound to `turn_end` fire.
   - Websocket subscription — confirm new event types appear in the stream
     (not silently dropped by broadcaster).
7. No full suite run (CLAUDE.md: 11k tests, >30 min).
