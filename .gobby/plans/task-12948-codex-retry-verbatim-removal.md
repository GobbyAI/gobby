# Kill Codex Retry-Verbatim PreToolUse UX

## Overview

Codex CLI users hit a loop where a `PreToolUse` hook blocks an MCP tool call with a message of the form *"Retry this tool call by resending the corrected input below verbatim. Do not add, remove, or rename fields."* The "corrected input" is structurally indistinguishable from what the agent already sent — e.g. it leaves `session_id` inside `arguments` for `gobby-skills:get_skill`, contradicting the canonical wrapper-only pattern. Codex obeys, resends verbatim, and either retriggers the same hook or produces a call that violates project policy. Multiple commits patched symptoms (see `_is_wrapper_only_call_tool_rewrite` in `src/gobby/adapters/codex_impl/hooks_adapter.py`) without addressing the root cause: the MCP proxy at `src/gobby/mcp_proxy/server.py:231-258` already calls `resolve_and_seed_contexts()` to translate `#N` → UUID server-side before invoking the inner tool, so the hook-layer synthesis of `response.modified_input` from `_session_refs_resolved` is redundant. For Claude Code that synthesis becomes `updatedInput` (harmless redundancy); for Codex 0.120.0, which rejects `updatedInput`, it triggers the broken retry-message UX. This plan removes both the synthesis and the Codex retry-message path.

## Constraints

- **Do not change** the proxy-layer session resolution at `src/gobby/mcp_proxy/server.py:231-258` — it is authoritative and correct.
- **Do not change** `src/gobby/mcp_proxy/_call_tool_wrapper.py::canonicalize_call_tool_wrapper` — used by the proxy and chat-session helpers.
- **Do not change** Claude Code's `updatedInput` path at `src/gobby/adapters/claude_code.py:260-286` — it is still valid for legitimate workflow-driven rewrites (`workflows/engine/effects.py::rewrite_input`).
- **Do not change** the canonical project-memory pattern (`session_id` is wrapper context only; not in inner `get_skill` arguments).
- **Keep** the in-place mutation of `event.data["tool_input"]` at `src/gobby/hooks/hook_manager.py:541` — telemetry, broadcaster, and downstream rule evaluation rely on seeing UUID-resolved values, but it must stop driving `response.modified_input`.
- The reasoning of the user's complaint is explicit: no rewrite suggestions to Codex of any kind. Even if a "corrected" input could be computed, Codex cannot apply it faithfully and the suggestion contradicts canonical patterns.

## Phase 1: Stop synthesizing modified_input from session-ref resolution

**Goal**: Remove the hook-manager block that copies `event.data["tool_input"]` into `response.modified_input` whenever `_session_refs_resolved` was set; the proxy is authoritative for `#N` → UUID translation.

### 1.1 Drop `_session_refs_resolved` → `modified_input` synthesis [category: code]

Target: `src/gobby/hooks/hook_manager.py`

In `handle_event` (the post-processing common block, currently at lines ~400-417), delete the synthesis block:

```python
# If we resolved #N session refs but no rule/coercion set _modified_input,
# create one so Claude Code sends UUIDs to the MCP server
if event.metadata.pop("_session_refs_resolved", False):
    if not response.modified_input:
        response.modified_input = event.data.get("tool_input", {})
        response.auto_approve = True
```

In `_resolve_session_refs_in_tool_input` (currently at lines ~482-548), delete the flag setter at the end:

```python
if replay_needed:
    event.metadata["_session_refs_resolved"] = True
```

Drop the `replay_needed` accumulator entirely (currently lines 501, 512, 518) since nothing consumes it after the flag setter goes. The `_try_resolve_session_field` helper continues to mutate the dict in place (line 541 — `d[field] = resolved`), and that mutation continues to be useful: rules evaluating after `_resolve_session_refs_in_tool_input` see UUID-resolved values; the broadcaster (`src/gobby/hooks/broadcaster.py:226-250`) and the chat-session UI both render the resolved form.

After this change:

- For **Claude Code**: `updatedInput` no longer fires from session-ref resolution alone. The proxy resolves `#N` on receipt at `mcp_proxy/server.py:231` (`resolve_and_seed_contexts`) so the call still succeeds first try with no user-visible change. Workflow rules that legitimately set `_modified_input` via `event.metadata` (`hook_manager.py:408-410`) continue to flow through to `updatedInput` — that path is untouched.
- For **Codex**: the synthesis no longer feeds the broken retry-message path in Phase 2. Same proxy-side resolution applies.

The existing regression test at `tests/mcp_proxy/services/test_call_tool_session_id_context.py:101-106` already asserts that the proxy forwards a resolved UUID to the inner tool; nothing about that contract changes here.

**Test scenarios the [TDD] wrapper should cover** (the wrapper auto-generates failing tests before this implementation lands):

- A `BEFORE_TOOL` event for `mcp__gobby__call_tool` whose `tool_input.arguments.session_id == "#3"` is fed to `HookManager.handle_event`. After processing, assert `response.modified_input is None`, `response.auto_approve is False`, and `event.data["tool_input"]["arguments"]["session_id"]` equals the resolved UUID. Assert `event.metadata.get("_session_refs_resolved")` is absent.
- A `BEFORE_TOOL` event for `mcp__gobby__list_tools` with top-level `session_id == "#3"`. Same assertions: `response.modified_input is None`, `event.data["tool_input"]["session_id"]` is the UUID, no `_session_refs_resolved` flag.
- A `BEFORE_TOOL` event with no `#N` references at all. `response.modified_input is None`, no mutation, no flag.

Validation criteria: `uv run pytest tests/hooks/ -v -k "session_ref or session_resolution"` is green and includes the three scenarios above. Grep on `src/gobby/hooks/hook_manager.py` for the literal string `_session_refs_resolved` returns zero matches. `uv run ruff check src/gobby/hooks/hook_manager.py` and `uv run mypy src/gobby/hooks/hook_manager.py` are clean.

## Phase 2: Delete the Codex retry-message UX

**Goal**: Remove the `PreToolUse` retry-block path from the Codex adapter and the now-unreachable `_is_wrapper_only_call_tool_rewrite` helper. Codex hooks block cleanly with a plain `systemMessage` or fall through to `allow`; they never suggest a "retry verbatim" payload.

### 2.1 Remove retry-message block and dead helper [category: code] (depends: Phase 1)

Target: `src/gobby/adapters/codex_impl/hooks_adapter.py`

Delete the entire retry-message block (currently lines ~166-210) inside `translate_from_hook_response`:

```python
# Codex CLI 0.120.0 rejects ``updatedInput`` and ``permissionDecision=allow``
# for PreToolUse hooks. When Gobby wants to rewrite a tool call, block the
# current execution and tell the model exactly how to retry instead.
has_retry_signal = bool(
    response.auto_approve
    or normalized_reason
    or response.context
    or response.system_message
)
if (
    response.modified_input
    and response.decision not in ("deny", "block")
    and hook_event_name == "PreToolUse"
    and has_retry_signal
    and not self._is_wrapper_only_call_tool_rewrite(response)
):
    retry_reason = (
        normalized_reason
        or "Retry the tool call by resending the corrected input from the hook message "
        "verbatim. Do not reformulate it."
    )
    retry_parts: list[str] = []
    if response.system_message:
        retry_parts.append(response.system_message)
    if response.context:
        retry_parts.append(response.context)
    retry_parts.append(
        "Retry this tool call by resending the corrected input below verbatim. "
        "Do not add, remove, or rename fields.\n"
        f"{json.dumps(response.modified_input, indent=2, sort_keys=True)}"
    )
    retry_result: dict[str, Any] = {
        "decision": "block",
        "reason": retry_reason,
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": retry_reason,
        },
    }
    retry_result["systemMessage"] = truncate_additional_context(
        "\n\n".join(retry_parts),
        contributor_sizes={
            f"retry_part_{idx}": len(part) for idx, part in enumerate(retry_parts, start=1)
        },
        logger=logger,
    )
    return retry_result
```

Delete the now-unused helper `_is_wrapper_only_call_tool_rewrite` (currently lines 64-102). It is the only consumer of `canonicalize_call_tool_wrapper` / `CallToolWrapperInputError` in this file, so also delete the imports (currently lines 21-24):

```python
from gobby.mcp_proxy._call_tool_wrapper import (
    CallToolWrapperInputError,
    canonicalize_call_tool_wrapper,
)
```

The `json` and `truncate_additional_context` imports become unused too — delete those if no other call site in the file uses them. Confirm by grepping the file post-change.

After these deletions, the next code path in `translate_from_hook_response` is the `if response.decision in ("deny", "block")` handler (currently line 212+). For PreToolUse responses where the upstream hook set `decision="allow"` and also a `modified_input` (e.g. a workflow rule's rewrite that targeted a Claude Code session), Codex doesn't support input rewriting, so the response should fall through to the standard allow path. The agent's original input is what Codex sent on the wire; the proxy's runtime resolution of `#N` (`mcp_proxy/server.py:231`) still applies, so calls are still correct.

For observability, add a single debug-level log immediately before the existing `if response.decision in ("deny", "block")` check:

```python
if response.modified_input is not None and hook_event_name == "PreToolUse":
    logger.debug(
        "Codex PreToolUse hook returned modified_input; Codex does not support "
        "updatedInput. Falling through to %s decision.",
        response.decision or "allow",
    )
```

This is observable in `~/.gobby/logs/gobby.log` at debug level but never reaches the user.

**Test scenarios the [TDD] wrapper should cover** (added to `tests/adapters/test_codex_call_tool_session_id.py`; the existing test in that file stays as-is and continues to assert no retry phrase for the wrapper-only case):

1. `test_nested_arguments_session_resolution_does_not_emit_retry_block`: build a `HookResponse` representing a `mcp__gobby__call_tool` PreToolUse where the agent passed `arguments={"name": "brevity", "session_id": "#3"}` and the hook resolved `#3` to a UUID. Per Phase 1, `response.modified_input` is `None` after the hook manager runs, so Phase 2's deletion is implicitly verified by the absence of the retry block. The test should construct the response post-Phase-1 (no `modified_input`) and assert that `translate_from_hook_response(response, hook_type="PreToolUse")` returns no `decision: block` and no `systemMessage` containing `"Retry this tool call"` or `"resending the corrected input"`.

2. `test_workflow_modified_input_for_codex_falls_through_to_allow`: build a `HookResponse` with `decision="allow"`, `modified_input={"server_name": "gobby-tasks", "tool_name": "create_task", "arguments": {"title": "...", "category": "code"}}`, no `_session_refs_resolved` ever set. Assert `translate_from_hook_response` returns the standard allow shape (`{"continue": True, ...}`), no `decision: block`, no retry phrasing.

3. `test_user_repro_get_skill_call_tool_with_nested_session_id`: replicate the user's exact failing trace — a PreToolUse for `mcp__gobby__call_tool` targeting `gobby-skills:get_skill` with `arguments={"name": "brevity", "session_id": "0c64f1e4-ef3e-46ee-8d5e-ad322e04b93c"}` (UUID, not `#N`, mirroring Codex's actual behavior post-resolution). Run through the full `HookManager` if a fixture is available (e.g. `tests/hooks/conftest.py::hook_manager`); otherwise stub the response. Assert the Codex adapter output contains none of the three forbidden phrases (`"Retry this tool call by resending the corrected input"`, `"resending the corrected input from the hook message verbatim"`, `"Do not add, remove, or rename fields"`).

Validation criteria: `uv run pytest tests/adapters/test_codex_call_tool_session_id.py -v` passes with at least four tests (one existing + three new), all green. Grep on `src/gobby/adapters/codex_impl/hooks_adapter.py` for any of the forbidden phrases returns zero matches. `uv run ruff check src/gobby/adapters/codex_impl/hooks_adapter.py` and `uv run mypy src/gobby/adapters/codex_impl/hooks_adapter.py` are clean. The file no longer imports `canonicalize_call_tool_wrapper`, `CallToolWrapperInputError`, or `json` (unless a remaining callsite uses `json`).

## Phase 3: Reconcile pre-existing test assertions

**Goal**: Find every test in the codebase that asserted the old retry-message behavior and invert it to forbid the phrases. Without this, Phase 2's deletion will fail tests that were locking in the broken UX.

### 3.1 Invert retry-phrase assertions in test_codex.py [category: refactor] (depends: Phase 2)

Target: `tests/adapters/test_codex.py`

There is at least one test in this file (around lines 1981-2080 per investigation) that asserts the old retry phrasing appears in the Codex hook output. Locate every such assertion. For each:

- If the assertion is `assert "Retry this tool call by resending..." in result["systemMessage"]` (or any of the three forbidden phrases, listed below), invert it to `assert "..." not in result.get("systemMessage", "")`.
- If a test was named or scoped specifically around the retry behavior (e.g. `test_modified_input_emits_retry_block_for_codex`), rename it to reflect the new contract (e.g. `test_modified_input_does_not_emit_retry_block_for_codex`) and rewrite the assertions consistently. Prefer rewriting over deletion — explicit forbid-coverage protects against regressions.
- If a test exists solely to verify the retry block (no other assertions worth keeping), repurpose it: same setup, but assert the new contract (no retry block, plain `allow` or `block` decision depending on the response).

Forbidden phrases to forbid in this and all other test files going forward:

- `"Retry this tool call by resending the corrected input"`
- `"resending the corrected input from the hook message verbatim"`
- `"Do not add, remove, or rename fields"`

Run `grep -rn 'Retry this tool call\|resending the corrected input\|Do not add, remove, or rename fields' tests/` to confirm coverage. If any other test file (besides `test_codex.py` and `test_codex_call_tool_session_id.py`) contains these phrases, apply the same inversion.

Validation criteria: `uv run pytest tests/adapters/test_codex.py -v` is green. `grep -rn 'Retry this tool call\|resending the corrected input' tests/` shows the three forbidden phrases only inside `not in` style assertions, never inside `in` style assertions. `uv run ruff check tests/adapters/test_codex.py` is clean.

## Phase 4: Manual end-to-end verification

**Goal**: Reproduce the user's exact failing scenario in a real Codex CLI session and confirm it succeeds without any retry-message blocks. Sanity-check Claude Code is unaffected.

### 4.1 Reproduce and verify the failing scenario [category: manual] (depends: Phase 3)

Steps:

1. Apply all changes from Phases 1-3 and confirm the targeted test suite is green:

   ```bash
   uv run pytest tests/adapters/test_codex.py tests/adapters/test_codex_call_tool_session_id.py tests/mcp_proxy/services/test_call_tool_session_id_context.py tests/hooks/ -v -k "session or modified_input or rewrite or retry"
   ```

2. Restart the daemon so the CLI hook adapters pick up the new code:

   ```bash
   uv run gobby restart
   uv run gobby status
   ```

   Confirm status is `running`.

3. From a Codex CLI session attached to this Gobby daemon, start a fresh conversation. The SessionStart hook emits the `Call get_skill(name="<skill>") on gobby-skills, then continue.` directive (e.g. for `brevity`). Codex must call `mcp__gobby__call_tool` with `server_name="gobby-skills"`, `tool_name="get_skill"`, and `arguments` containing at minimum `{"name": "brevity"}`. The call should succeed first try.

4. Inspect Codex's transcript. It MUST NOT contain any block of the form:

   - `"Retry this tool call by resending the corrected input below verbatim."`
   - `"resending the corrected input from the hook message verbatim. Do not reformulate it."`
   - `"Do not add, remove, or rename fields."`

5. Repeat the same scenario for a Claude Code session: start a fresh conversation, observe the same skill-fetch directive being honored, confirm no user-visible change. The proxy resolves any `#N` references on receipt; `updatedInput` is no longer emitted from session resolution but the call still succeeds first try.

6. Watch `~/.gobby/logs/gobby.log` during both sessions for the new debug log added in Phase 2 (`"Codex PreToolUse hook returned modified_input; Codex does not support updatedInput..."`). It should appear zero times for normal session flow, and only ever fire if a workflow rule legitimately produces a `modified_input` for a Codex PreToolUse — which currently no bundled rule does.

Validation criteria: a Codex session and a Claude Code session both fetch a skill via the canonical SessionStart directive without any blocked tool calls, no retry-message systemMessages anywhere in either transcript, and the targeted test suite from step 1 passes locally. Document the observed CLI versions (Codex `--version`, Claude Code `--version`) in the closing commit body.

## Task Mapping

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
