# Kill Codex Retry-Verbatim PreToolUse UX

## Overview

Codex CLI users hit a loop where a `PreToolUse` hook blocks an MCP tool call with a message of the form *"Retry this tool call by resending the corrected input below verbatim. Do not add, remove, or rename fields."* The "corrected input" is structurally indistinguishable from what the agent already sent — e.g. it leaves `session_id` inside `arguments` for `gobby-skills:get_skill`, contradicting the canonical wrapper-only pattern. Codex obeys, resends verbatim, and either retriggers the same hook or produces a call that violates project policy. Multiple commits patched symptoms (see `_is_wrapper_only_call_tool_rewrite` in `src/gobby/adapters/codex_impl/hooks_adapter.py`) without addressing the root cause.

The actual architecture is:

1. **The proxy applies `modified_input` for any rule that fires server-side.** `src/gobby/mcp_proxy/services/result_handling.py:274-347::apply_before_tool_enforcement` is invoked from the universal proxy chokepoint `src/gobby/mcp_proxy/services/tool_execution.py:132::call_tool` (lines 186-200). It evaluates `before_tool` workflow rules and applies any `modified_input` directly to the dispatched arguments (lines 334-347). For Codex, even when the CLI ignores `updatedInput`, the proxy will rewrite the call before execution **for any rule whose condition actually matches the proxy-side event shape**. The hook-layer "retry verbatim" UX was therefore cosmetic for those cases; deleting it doesn't change observed behavior. (See Constraints — the bundled `strip-skip-validation-with-commit` rule's condition does *not* currently match the proxy event shape for Codex via call_tool; that's a pre-existing gap tracked separately and explicitly out of scope here.)
2. **The proxy resolves wrapper-level `session_id` (`#N` → UUID) but not nested `arguments.session_id`.** `mcp_proxy/server.py::GobbyDaemonTools.call_tool:231-258` runs `resolve_and_seed_contexts()` only with the wrapper-level `session_id`; `canonicalize_call_tool_wrapper` (line 99) deliberately leaves `arguments.session_id` as a target-tool parameter; `tool_proxy.call_tool` forwards `arguments` unchanged. The hook layer currently masks this gap by mutating `event.data["tool_input"]` in place and synthesizing `response.modified_input`, signaling Claude Code to re-send via `updatedInput`. That mask is what feeds the broken Codex retry-message UX. Stdio (`mcp_proxy/stdio.py::DaemonProxy.call_tool`) and direct REST (`servers/routes/mcp/endpoints/execution.py`) bypass `GobbyDaemonTools.call_tool` and POST straight to `tool_proxy.call_tool` → `tool_execution.call_tool` — so any new resolver must live there to catch every public path.

The fix:

- **Resolve nested `arguments.session_id` at the proxy chokepoint** (`tool_execution.call_tool`), not just the wrapper. This makes the proxy authoritative for both wrapper and nested session refs across every public transport.
- **Stop synthesizing `response.modified_input` from `_session_refs_resolved`.** Once proxy resolution covers nested args, the hook synthesis is redundant. The in-place mutation of `event.data["tool_input"]` stays for telemetry and rule evaluation; only the `modified_input` synthesis goes away.
- **Delete the Codex retry-message UX entirely.** Codex `PreToolUse` responses with `modified_input` (from any workflow `rewrite_input` rule that fires for the call) fall through to the existing `decision="allow"` branch at `codex_impl/hooks_adapter.py:248+`, which already surfaces `response.context` and `response.system_message` via `systemMessage`. The proxy applies the actual rewrite via `apply_before_tool_enforcement`. Codex sees informative context, proceeds with its original call, and the rewrite is enforced server-side before dispatch. No "retry verbatim" payload, no JSON, no loop.

## Constraints

- **Do not change** Claude Code's `updatedInput` path at `src/gobby/adapters/claude_code.py:260-286`. After Phase 2 it no longer fires from session-ref resolution alone (proxy resolution covers it), but workflow-driven `_modified_input` from `event.metadata` (lines 408-410) still flows through to `updatedInput`. Claude Code remains transparent for legitimate rewrites.
- **Do not change** the proxy's existing behavior in `apply_before_tool_enforcement` (`result_handling.py:274-347`). It already enforces input rewrites correctly; this plan adds coverage but does not modify the function.
- **Keep** the in-place mutation of `event.data["tool_input"]` at `src/gobby/hooks/hook_manager.py:541`. Telemetry, broadcaster, and downstream rule evaluation rely on UUID-resolved values being visible in the event. The mutation just stops driving `response.modified_input`.
- **Variable tools.** The existing hook-layer comment at `hook_manager.py:503-505` notes variable tools (`mcp__gobby__set_variable`, `mcp__gobby__get_variable`) "intentionally keep the user's explicit session ref" — but this exception applies only to top-level invocations of those tools (`tool_name == "mcp__gobby__set_variable"`), not to `mcp__gobby__call_tool` with an inner variable tool, which the hook already resolves unconditionally (`hook_manager.py:514-518` has no variable-tool exception). Top-level variable tool calls (`mcp__gobby__set_variable`) are FastMCP top-level shortcuts and do not route through `tool_execution.call_tool`, so the new proxy-layer resolver naturally never sees them. Inner variable tools reached via `mcp__gobby__call_tool` are resolved unconditionally to match existing hook behavior. **No variable-tool exception is added at the proxy layer.** The variable-tool schemas accept both `#N` and UUID, so resolution does not break them.
- **No retry-verbatim UX.** The user's complaint is explicit. Codex never receives a "resend this JSON verbatim" suggestion from any code path after this plan lands. Block decisions and informative context are still allowed, since they are human-readable enforcement, not rewrite suggestions.
- **Out of scope: bundled `strip-skip-validation-with-commit` rule reconciliation.** This rule's `when:` clause checks `event.data.get('tool_name') == 'close_task'`. For `mcp__gobby__call_tool` targeting `gobby-tasks:close_task`, the proxy-side synthetic event built by `result_handling.build_before_tool_event` has `event.data["tool_name"] == "mcp__gobby__call_tool"` (the wrapper, not the inner tool), so the condition is false and the rule does **not** fire for the Codex/proxy path today. `TemplatingMixin._build_eval_context` (`workflows/engine/templating.py:50-65`) unwraps inner `arguments` into `tool_input` and re-injects the inner `tool_name`, but the existing rule condition references `event.data.tool_name`, not `tool_input.tool_name`, so the unwrap doesn't help. This is a **pre-existing safety gap**: the skip_validation rewrite isn't enforced today for Codex calling `close_task` via `call_tool` (the path Codex normally uses). It is separate from the user's retry-verbatim complaint and is not addressed by this plan. **Follow-up task `#13048`** tracks the fix. This plan neither preserves nor breaks the contract for that path — it leaves the existing state as-is.

## Partial-merge risk

Phases must land in order. The dependency edges (`1.1 → 2.1 → 3.1 → 4.1 → 5.1`) are not optional ordering hints; out-of-order merges introduce real bugs:

- **Phase 2 ahead of Phase 1.** Removing the hook-layer `_session_refs_resolved` → `modified_input` synthesis without first adding proxy-layer nested-args resolution would break nested `#N` refs entirely. Codex (which already ignores `updatedInput`) would send `arguments.session_id="#3"` literally to the inner tool, which has no idea what to do with it. Claude Code would also break, because `updatedInput` is the only thing currently rewriting nested `#N` to UUID for that adapter. The user's retry-UX symptom would be replaced by a worse hard-failure symptom.
- **Phase 3 ahead of Phase 2.** Deleting the Codex retry-message path while the hook still synthesizes `modified_input` from `_session_refs_resolved` is less catastrophic — the synthesis would simply have no Codex consumer. But the synthesis would still fire for Claude Code's `updatedInput`, producing redundant re-issues (Phase 1's proxy resolution makes them unnecessary). The fall-through would surface `response.context` for Codex correctly, so the user-facing UX would already be fixed; this is the least-bad partial-merge state. Still avoid it: dead synthesis behavior in `hook_manager.py` is a regression vector for future maintainers.
- **Phase 4 ahead of Phase 3.** Inverting test assertions before deleting the retry-block code would fail the inverted tests on `main`. Tests must follow code, not lead it.

Operational consequence: if landing this as multiple PRs, each PR must include its phase's tests so CI catches partial-merge attempts. If landing as one PR, the phase ordering inside the PR commit history is informational; the PR-level invariant is that the diff hits all five sections together.

## Phase 1: Resolve nested `arguments.session_id` at the proxy chokepoint

**Goal**: Resolve `#N` references in `arguments.session_id` at the universal proxy chokepoint `tool_execution.call_tool`, so every public call path (server.py wrapper, direct REST, stdio→REST) sees UUIDs at the inner tool. Extract the existing hook helper into a shared utility so both layers use the same code.

### 1.1 Create shared session-ref helper and wire it into `tool_execution.call_tool` [category: code]

Target files:
- `src/gobby/utils/session_refs.py` (new)
- `src/gobby/hooks/hook_manager.py` (refactor `_try_resolve_session_field` to delegate)
- `src/gobby/mcp_proxy/services/tool_execution.py` (call the helper in `call_tool`)

**Step A — extract the helper.** Create `src/gobby/utils/session_refs.py`. The session manager type is `gobby.hooks.session_types.HookSessionManager` (the protocol used by hook code) or `gobby.storage.sessions.SessionManager` (the concrete storage class) — pick `HookSessionManager` since both call sites are protocol-shaped and avoiding the storage import keeps the helper transport-agnostic. Use `TYPE_CHECKING` to avoid an import cycle:

```python
"""Shared helpers for resolving #N session references in tool inputs."""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.hooks.session_types import HookSessionManager

logger = logging.getLogger(__name__)


def try_resolve_session_field(
    container: dict[str, Any],
    field: str,
    *,
    session_manager: "HookSessionManager | None",
    project_id: str | None,
) -> bool:
    """Resolve a #N session reference in container[field] to UUID in place.

    Returns True if the field was rewritten. Returns False (without touching
    the dict) when the value is missing, not a string, already a UUID, or
    when no session_manager is available. Safe to call on any container —
    runs no-op when the field is absent.
    """
    if session_manager is None:
        return False

    val = container.get(field)
    if not isinstance(val, str):
        return False

    ref = val.lstrip("#") if val.startswith("#") else val
    if not ref.isdigit():
        return False

    try:
        resolved = session_manager.resolve_session_reference(val, project_id)
    except ValueError as e:
        logger.debug("Could not resolve session ref %r: %s", val, e)
        return False
    except Exception as e:
        logger.warning("Unexpected error resolving session ref %r: %s", val, e, exc_info=True)
        return False

    if resolved == val:
        return False

    container[field] = resolved
    return True
```

**Step B — refactor `hook_manager._try_resolve_session_field` to delegate.** Replace the existing implementation (currently `src/gobby/hooks/hook_manager.py:523-548`) with a thin delegate to the new helper. Add the import at the top of `src/gobby/hooks/hook_manager.py` alongside the other `gobby.utils.*` imports — the helper itself uses only `TYPE_CHECKING` for the manager type, so a top-level import here introduces no circular dependency:

```python
# top of src/gobby/hooks/hook_manager.py, alongside other gobby imports
from gobby.utils.session_refs import try_resolve_session_field
```

```python
def _try_resolve_session_field(
    self, d: dict[str, Any], field: str, project_id: str | None
) -> bool:
    """Resolve a #N session reference in d[field] to UUID in place.

    Delegates to gobby.utils.session_refs.try_resolve_session_field; kept as
    an instance method so existing call sites in this module compose with
    self._session_manager naturally.
    """
    return try_resolve_session_field(
        d,
        field,
        session_manager=self._session_manager,
        project_id=project_id,
    )
```

**Step C — wire the helper into `tool_execution.call_tool`.** Insert resolution after `arguments` is guaranteed to be a dict and before the proxy-namespace recursion. In `src/gobby/mcp_proxy/services/tool_execution.py::call_tool`, after the line `arguments = cast("dict[str, Any]", prepared_arguments or {})` (currently line 161) and before the proxy-namespace check (currently line 163), insert:

```python
# Resolve nested arguments.session_id (#N -> UUID) at the universal proxy
# chokepoint. The wrapper-level session_id is already resolved upstream
# (server.py::GobbyDaemonTools.call_tool runs resolve_and_seed_contexts);
# direct REST/stdio paths land here without that step. Resolution is
# unconditional — it matches the existing hook-layer behavior at
# hook_manager.py:514-518 for nested call_tool args, where no variable-tool
# exception is applied.
if isinstance(arguments, dict):
    hook_manager = service._resolve_hook_manager()
    session_manager = (
        getattr(hook_manager, "_session_manager", None) if hook_manager else None
    )
    # Project scope: prefer the contextvar-seeded project (set upstream by
    # resolve_and_seed_contexts at server.py:231 for wrapper calls), then
    # fall back to the proxy's MCP manager scope. Match how
    # apply_before_tool_enforcement resolves session/project in
    # result_handling.py:282 + session_context.py.
    #
    # NOTE: get_project_context() returns a project context dict (or None),
    # not a string. The dict has an "id" key for the project UUID. Extract
    # it explicitly — passing the raw dict to try_resolve_session_field
    # would propagate as project_id to SessionManager.resolve_session_reference,
    # which uses it as a SQL parameter, and the helper's broad except
    # Exception would silently swallow the type failure.
    from gobby.utils.project_context import get_project_context
    project_ctx = get_project_context()
    project_ref = (
        (project_ctx.get("id") if isinstance(project_ctx, dict) else None)
        or getattr(getattr(service, "_mcp_manager", None), "project_id", None)
    )
    try_resolve_session_field(
        arguments,
        "session_id",
        session_manager=session_manager,
        project_id=project_ref,
    )
```

The exact accessors used here are taken from existing call sites in the proxy: `service._resolve_hook_manager()` is already used at `result_handling.py:286` (apply_before_tool_enforcement); the resulting `hook_manager._session_manager` is the same `HookSessionManager` instance the hook layer uses for `#N` resolution. `service._mcp_manager.project_id` is the proxy's container scope (`MCPClientManager.project_id`). `get_project_context()` from `gobby.utils.project_context` is what `session_context.py` already uses to read the contextvar-seeded project for the current call. None of these are new APIs — they are pre-existing on the codebase as of this plan.

Add the import at the top of `tool_execution.py`:

```python
from gobby.utils.session_refs import try_resolve_session_field
```

**Why insert here and not later** (e.g. after `_apply_before_tool_enforcement`): workflow rules evaluated at the proxy via `_apply_before_tool_enforcement` may inspect `arguments.session_id` in their `when:` conditions. Resolution must happen first so rules see UUIDs, matching the hook-layer ordering.

**Why not also insert at `mcp_proxy/server.py::GobbyDaemonTools.call_tool`**: that function calls `self.tool_proxy.call_tool(...)` which routes to `tool_execution.call_tool`, so a single insertion at `tool_execution.call_tool` covers it. The wrapper-level `session_id` is still resolved at `server.py:231` via `resolve_and_seed_contexts` (different parameter, different concern).

**Test scenarios the [TDD] wrapper should cover** (in `tests/mcp_proxy/services/test_call_tool_session_id_context.py`, alongside existing wrapper-level coverage):

- Direct call to `tool_execution.call_tool(service, server_name="gobby-skills", tool_name="get_skill", arguments={"name": "brevity", "session_id": "#3"}, session_id=None)` resolves `arguments["session_id"]` to a known UUID before invoking the dispatch path. Mock the dispatcher and assert on captured arguments.
- Same call with `arguments={"session_id": "<already-uuid>"}`: no mutation; UUID passes through.
- Same call with `arguments={}`: no mutation; no error.
- Same call with `arguments=None` (the `prepared_arguments or {}` cast turns this into `{}`): no mutation; no error.
- Wrapper-level `session_id="#3"` AND nested `arguments.session_id="#7"`: the wrapper-level resolution happens upstream (in `server.py::call_tool`); the nested resolution happens here. Drive both via `GobbyDaemonTools.call_tool` end-to-end and assert both fields land as UUIDs at dispatch.
- **Stdio path coverage.** Drive a request through `mcp_proxy/stdio.py::DaemonProxy.call_tool` (or its REST-POST equivalent) with `arguments={"session_id": "#3"}`. Assert the dispatch sees the resolved UUID. This proves the resolver is reached via every public transport, not only the FastMCP wrapper. Use the existing stdio test fixture if present (`tests/mcp_proxy/transports/` or similar); add a fixture if absent.
- **REST path coverage.** Same payload via `servers/routes/mcp/endpoints/execution.py` (or its `POST /api/mcp/{server_name}/tools/{tool_name}` route). Assert dispatch sees the resolved UUID.
- **Variable-tool preservation via top-level call.** A top-level call to `mcp__gobby__set_variable({session_id: "#3", ...})` (which is a FastMCP top-level shortcut, not routed through `tool_execution.call_tool`) preserves `session_id="#3"` end-to-end. This test confirms the new resolver did not accidentally intercept the top-level shortcut. If a fixture exists, drive through the actual handler; otherwise assert by reading the registration code that the handler does not call `tool_execution.call_tool`.

Validation criteria: `uv run pytest tests/mcp_proxy/services/test_call_tool_session_id_context.py tests/mcp_proxy/transports/ -v` passes with the new cases; `uv run ruff check src/gobby/utils/session_refs.py src/gobby/mcp_proxy/services/tool_execution.py src/gobby/hooks/hook_manager.py` is clean; `uv run mypy src/gobby/utils/session_refs.py src/gobby/mcp_proxy/services/tool_execution.py src/gobby/hooks/hook_manager.py` is clean; `try_resolve_session_field` has exactly one definition (the new shared helper); `hook_manager.py::_try_resolve_session_field` is a thin delegate.

## Phase 2: Stop synthesizing `modified_input` from session-ref resolution

**Goal**: Now that the proxy resolves `#N` server-side for both wrapper and nested args, the hook-layer synthesis of `response.modified_input` from `_session_refs_resolved` is redundant. Drop it.

### 2.1 Remove `_session_refs_resolved` → `modified_input` synthesis [category: code] (depends: Phase 1)

Target: `src/gobby/hooks/hook_manager.py`

In `_handle_internal` (the post-processing common block, currently at lines ~400-417 — `_handle_internal` is the actual method; `handle` wraps it with session-lookup and health-check guards), delete the synthesis block:

```python
# If we resolved #N session refs but no rule/coercion set _modified_input,
# create one so Claude Code sends UUIDs to the MCP server
if event.metadata.pop("_session_refs_resolved", False):
    if not response.modified_input:
        response.modified_input = event.data.get("tool_input", {})
        response.auto_approve = True
```

In `_resolve_session_refs_in_tool_input` (currently at lines ~482-548), delete the flag setter:

```python
if replay_needed:
    event.metadata["_session_refs_resolved"] = True
```

Drop the `replay_needed` accumulator entirely (currently lines 501, 512, 518) — nothing consumes it. The `_try_resolve_session_field` delegate continues to mutate the dict in place via the new shared helper from Phase 1, and that mutation continues to be useful: rules evaluating after `_resolve_session_refs_in_tool_input` see UUID-resolved values; the broadcaster (`src/gobby/hooks/broadcaster.py:226-250`) and the chat-session UI both render the resolved form.

After this change:

- For **Claude Code**: `updatedInput` no longer fires from session-ref resolution alone. Phase 1's proxy resolution makes the call succeed first try with no user-visible change. Workflow rules that legitimately set `_modified_input` via `event.metadata` (`hook_manager.py:408-410`) continue to flow through to `updatedInput` — that path is untouched.
- For **Codex**: the synthesis no longer feeds the broken retry-message path. Phase 3 then deletes the path itself.

`_session_refs_resolved` has no consumers outside `hook_manager.py` (verified: `grep -rn '_session_refs_resolved' src/gobby/` returns only the four lines in `hook_manager.py` removed by this phase).

**Test scenarios the [TDD] wrapper should cover**:

- A `BEFORE_TOOL` event for `mcp__gobby__call_tool` whose `tool_input.arguments.session_id == "#3"` is fed to `HookManager.handle`. After processing: `response.modified_input is None`, `response.auto_approve is False`, `event.data["tool_input"]["arguments"]["session_id"]` is the resolved UUID, `event.metadata.get("_session_refs_resolved")` is absent.
- A `BEFORE_TOOL` event for `mcp__gobby__list_tools` with top-level `session_id == "#3"`. Same assertions: `response.modified_input is None`, `event.data["tool_input"]["session_id"]` is the UUID, no `_session_refs_resolved` flag.
- A `BEFORE_TOOL` event with no `#N` references. `response.modified_input is None`, no mutation, no flag.

Validation criteria: `uv run pytest tests/hooks/ -v -k "session_ref or session_resolution"` is green and includes the three scenarios. `grep -rn '_session_refs_resolved' src/gobby/` returns zero matches. `uv run ruff check src/gobby/hooks/hook_manager.py` and `uv run mypy src/gobby/hooks/hook_manager.py` are clean.

## Phase 3: Delete the Codex retry-message UX

**Goal**: Remove the `PreToolUse` retry-block path from the Codex adapter and the now-unreachable `_is_wrapper_only_call_tool_rewrite` helper. Codex `PreToolUse` responses with `modified_input` from genuine `rewrite_input` rules fall through to the existing `decision="allow"` branch, which already surfaces `response.context` and `response.system_message` as a clean `systemMessage`. The proxy enforces the actual input rewrite via `apply_before_tool_enforcement`. No "retry verbatim" payload anywhere.

### 3.1 Remove retry-message block, dead helper, and dead imports; add full-path regressions [category: code] (depends: Phase 2)

Target: `src/gobby/adapters/codex_impl/hooks_adapter.py`

Delete the entire retry-message block (currently lines ~166-210) inside `translate_from_hook_response`:

```python
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
    # ... entire block including json.dumps(response.modified_input, ...) ...
    return retry_result
```

Delete the now-unused helper `_is_wrapper_only_call_tool_rewrite` (currently lines 64-102). It is the only consumer of `canonicalize_call_tool_wrapper` / `CallToolWrapperInputError` in this file, so also delete the imports (currently lines 21-24):

```python
from gobby.mcp_proxy._call_tool_wrapper import (
    CallToolWrapperInputError,
    canonicalize_call_tool_wrapper,
)
```

The `json` import becomes unused (the only call was `json.dumps(response.modified_input, ...)` inside the deleted block). The `truncate_additional_context` import remains needed by the deny/block branch at line 233. Remove `json` (verify with grep on the file post-deletion).

After deletion the next code path inside `translate_from_hook_response` for `PreToolUse` responses with `decision="allow"` is line 248+, which already builds `result = {"continue": True}`, surfaces `response.context` (line 254-255 — `inject_context` effects from any `rewrite_input` rule that fires for the call), and surfaces `response.system_message` via `result["systemMessage"]` for `SYSTEM_MESSAGE_ONLY_EVENTS` (line 263-265). For the Codex agent this means: the call proceeds, the agent sees the `inject_context` text as a `systemMessage`, and the proxy's `apply_before_tool_enforcement` (`result_handling.py:274-347`) applies the actual `rewrite_input` to `arguments` before dispatch — verified by lines 334-347 of that function which read `modified_input` from a fresh proxy-side rule evaluation and substitute `server_name`, `tool_name`, `arguments` into the dispatched call. (See the Constraints section for the existing `strip-skip-validation-with-commit` rule's pre-existing gap on the `mcp__gobby__call_tool → close_task` path; that gap is out of scope for this plan.)

For observability, add a single debug-level log immediately before the existing `if response.decision in ("deny", "block")` check (currently line 212):

```python
if response.modified_input is not None and hook_event_name == "PreToolUse":
    logger.debug(
        "Codex PreToolUse hook returned modified_input; Codex does not "
        "support updatedInput. Proxy will apply rewrite at dispatch via "
        "apply_before_tool_enforcement. Decision=%s.",
        response.decision or "allow",
    )
```

**Test scenarios the [TDD] wrapper should cover** — placed in `tests/adapters/test_codex_call_tool_session_id.py`. The existing `test_wrapper_context_only_session_resolution_does_not_emit_retry_block` stays as-is. Add four new tests:

1. `test_nested_arguments_session_resolution_does_not_emit_retry_block` — pure adapter unit test. Build a `HookResponse` representing post-Phase-2 output for `mcp__gobby__call_tool` PreToolUse where the agent passed `arguments={"name": "brevity", "session_id": "#3"}`. Per Phase 2, `response.modified_input is None`. Assert `translate_from_hook_response(response, hook_type="PreToolUse")` returns `{"continue": True}`, no `decision: block`, no `systemMessage` containing any of the three forbidden phrases.

2. `test_workflow_modified_input_for_codex_falls_through_with_context` — pure adapter unit test that proves the adapter's behavior for *any* future or custom rule that produces a `modified_input` for a Codex PreToolUse. Build a `HookResponse` with `decision="allow"`, `modified_input={"command": "echo hello"}` (using the same top-level shape as the bundled bash rewrite rule's `input_updates` — note the bundled rule itself has `source != 'codex'` so it does not produce this for Codex today; the test uses the shape as a structural template, not as a real-world fire path), `context="Hypothetical Codex-targeting workflow rule rewrote a tool input."`. Assert `translate_from_hook_response` returns `{"continue": True}` (no `decision: block`), `result["systemMessage"]` contains the full context text verbatim, no retry phrasing in any field. Capture stderr/log via `caplog` and assert the new debug log fires exactly once.

3. `test_user_repro_get_skill_call_tool_with_nested_uuid` — **full-path** regression that drives `HookManager.handle` and then `CodexHooksAdapter.translate_from_hook_response`. The `hook_manager` fixture must be visible from `tests/adapters/test_codex_call_tool_session_id.py`; pytest fixture discovery is per-subtree, so a fixture in `tests/hooks/conftest.py` would not be reachable here. Place the fixture in **`tests/adapters/conftest.py`** (preferred — covers any sibling adapter test) or directly in the test file. If a similar fixture already exists at `tests/hooks/test_call_tool_session_id_refs.py` for a unit-style mock pattern, do not import it across subtrees — copy/relocate the construction into the new fixture.

   The fixture must give a fully-bootstrapped `HookManager` (rules synced, session registered, project context attached) so the proxy-side rule evaluation path actually fires. Minimum scaffolding:

   - `tmp_path`-scoped `LocalDatabase`, run migrations.
   - Create a project row and a `.gobby/project.json` so `get_project_context()` can resolve.
   - Run `sync_bundled_rules(db, get_bundled_rules_path())` so rule definitions land in `workflow_definitions`.
   - Construct a `DaemonConfig(database_path=...)` with webhooks disabled and external clients patched to no-ops.
   - Pre-warm or stub the health monitor so `HookManager.handle` doesn't reject the event on health grounds.
   - Register a Codex-source session with the project id; persist the session ref.
   - Build a `BEFORE_TOOL` `HookEvent` for `mcp__gobby__call_tool` with `tool_input` matching the user's payload.

   If the fully-bootstrapped fixture is too heavy and the test is intentionally bypassing session lookup / health checks, the alternative is to call `hook_manager._handle_internal(event)` and label the test as a hook-manager unit-style regression — explicit in the test docstring. The plan accepts either approach **as long as the test docstring clearly states which path it exercises**; reviewer should not have to read the body to know whether session/health gates fire. Construct a Codex `BEFORE_TOOL` event for `mcp__gobby__call_tool` targeting `gobby-skills:get_skill` with raw input matching the user's actual failing trace:

   ```python
   {
       "server_name": "gobby-skills",
       "tool_name": "get_skill",
       "arguments": {"name": "brevity", "session_id": "0c64f1e4-ef3e-46ee-8d5e-ad322e04b93c"},
   }
   ```

   (UUID, not `#N`.) Run through `hook_manager.handle(event)`, then through `CodexHooksAdapter.translate_from_hook_response(response, hook_type="PreToolUse")`. Assert `response.modified_input is None` and the adapter result contains none of the three forbidden phrases anywhere — neither in `systemMessage`, nor in `reason`, nor in `hookSpecificOutput.permissionDecisionReason`. **If this test fails**, that is the signal that some other code path (input coercion via `hooks/normalization.py:683`, an undiscovered rule, or another effect) is producing `modified_input` for the user's UUID-only trace. The implementing agent must trace the source, document it, and either fix it in this plan (preferred) or escalate with concrete evidence (file path, line, payload). Do not merge until this test passes; the plan is not done if the user's exact scenario still produces a retry block.

Validation criteria: `uv run pytest tests/adapters/test_codex_call_tool_session_id.py -v` passes with at least four tests (existing one + three new), all green. Grep on `src/gobby/adapters/codex_impl/hooks_adapter.py` for any of the forbidden phrases returns zero matches. `uv run ruff check src/gobby/adapters/codex_impl/hooks_adapter.py` and `uv run mypy src/gobby/adapters/codex_impl/hooks_adapter.py` are clean. The file no longer imports `canonicalize_call_tool_wrapper`, `CallToolWrapperInputError`, or `json`.

## Phase 4: Reconcile pre-existing test assertions

**Goal**: Find every test in the codebase that asserted the old retry-message behavior (positive `in` assertions) and invert it to forbid the phrases. Existing negative assertions (`not in`) are already correct and must be preserved.

### 4.1 Invert positive retry-phrase assertions; preserve existing negative ones [category: refactor] (depends: Phase 3)

Target: `tests/adapters/test_codex.py`, plus a sweep across `tests/`.

Forbidden phrases:

- `"Retry this tool call by resending the corrected input"`
- `"resending the corrected input from the hook message verbatim"`
- `"Do not add, remove, or rename fields"`

Two-pass procedure:

**Pass 1: classify existing hits.** Run:

```bash
grep -rn 'Retry this tool call\|resending the corrected input\|Do not add, remove, or rename fields' tests/
```

Expected hits (from Round 2 investigation):

- `tests/adapters/test_codex.py` — around lines 1981-2080. **Mixed**: positive `in` assertions that lock in the old retry behavior. **Invert these.**
- `tests/adapters/test_codex_call_tool_session_id.py` — already negative (`not in`). Preserve as-is; Phase 3 added more tests to this file.
- `tests/adapters/test_mcp_validation_errors.py` — already negative (`not in`). Preserve as-is.

For each hit, classify by assertion style: `assert "<phrase>" in <field>` is a positive assertion (forbidden — invert) and `assert "<phrase>" not in <field>` is a negative assertion (correct — preserve). If the surrounding test is named or scoped specifically around the retry behavior (e.g. `test_modified_input_emits_retry_block_for_codex`), rename it to reflect the new contract (`test_modified_input_does_not_emit_retry_block_for_codex`) and rewrite assertions consistently. Prefer rewriting over deletion — explicit forbid-coverage protects against regressions.

**Pass 2: enforce no positive assertions remain.** After Pass 1, re-run the grep and walk every hit. None should be inside `assert ... in` form.

Validation criteria: `uv run pytest tests/adapters/test_codex.py tests/adapters/test_codex_call_tool_session_id.py tests/adapters/test_mcp_validation_errors.py -v` is green. The grep above shows the three forbidden phrases only inside `not in` style assertions, never inside `in` style assertions. `uv run ruff check tests/adapters/test_codex.py` is clean.

## Phase 5: Manual end-to-end verification

**Goal**: Reproduce the user's exact failing scenario in a real Codex CLI session — including inner `session_id` in `arguments` — and confirm it succeeds without any retry-message blocks. Sanity-check Claude Code is unaffected.

### 5.1 Reproduce and verify the user's exact failing scenario [category: manual] (depends: Phase 4)

Steps:

1. Apply all changes from Phases 1-4 and confirm the targeted test suite is green:

   ```bash
   uv run pytest tests/adapters/test_codex.py \
                 tests/adapters/test_codex_call_tool_session_id.py \
                 tests/adapters/test_mcp_validation_errors.py \
                 tests/mcp_proxy/services/test_call_tool_session_id_context.py \
                 tests/mcp_proxy/transports/ \
                 tests/hooks/ \
                 -v -k "session or modified_input or rewrite or retry"
   ```

2. Restart the daemon so the CLI hook adapters and proxy pick up the new code:

   ```bash
   uv run gobby restart
   uv run gobby status
   ```

3. From a Codex CLI session attached to this Gobby daemon, start a fresh conversation. The SessionStart hook emits the canonical `Call get_skill(name="<skill>") on gobby-skills, then continue.` directive. Watch the transcript for Codex's `mcp__gobby__call_tool` invocation targeting `gobby-skills:get_skill`. **Force the failing shape** by either:
   - Inspecting the daemon log (`~/.gobby/logs/gobby.log`) for the actual MCP call payload Codex emitted; if `session_id` appears nested inside `arguments`, that's the user's exact trace.
   - Issuing a direct probe via curl against the local daemon. The daemon exposes two REST contracts (registered in `src/gobby/servers/routes/mcp/tools.py`); pick **one** and use the matching body shape:
     - **Wrapper endpoint** at `POST /api/mcp/tools/call` accepts a wrapper-shaped body. Use this to mimic the `mcp__gobby__call_tool` wrapper directly:
       ```json
       {
         "server_name": "gobby-skills",
         "tool_name": "get_skill",
         "arguments": {"name": "brevity", "session_id": "0c64f1e4-ef3e-46ee-8d5e-ad322e04b93c"}
       }
       ```
     - **Dynamic endpoint** at `POST /api/mcp/{server_name}/tools/{tool_name}` (handled by `src/gobby/servers/routes/mcp/endpoints/execution.py::mcp_proxy`) accepts the raw target-tool arguments as the body — there is no outer `arguments` wrapper. Use this to hit `gobby-skills:get_skill` directly:
       ```bash
       curl -X POST http://localhost:<port>/api/mcp/gobby-skills/tools/get_skill \
         -H "Content-Type: application/json" \
         -d '{"name": "brevity", "session_id": "0c64f1e4-ef3e-46ee-8d5e-ad322e04b93c"}'
       ```
     No auth header is required by either route. The `X-Gobby-Session-Id` and `X-Gobby-Project-Id` headers are optional and only set wrapper-level context; the wrapper endpoint also accepts `session_id` / `project_id` in the JSON body. Use a real session UUID for the inner `session_id`. Confirm the response is a successful skill payload, not a workflow block.

4. Inspect the Codex transcript and the daemon log. The transcript MUST NOT contain any block of the form:
   - `"Retry this tool call by resending the corrected input below verbatim."`
   - `"resending the corrected input from the hook message verbatim. Do not reformulate it."`
   - `"Do not add, remove, or rename fields."`
   The log MAY contain the new debug line `"Codex PreToolUse hook returned modified_input; Codex does not support updatedInput..."` if a workflow rule fired during the session.

5. Repeat step 3 for a Claude Code session: confirm no behavior change visible to the user. The proxy resolves `#N` on receipt for both wrapper and nested args; `updatedInput` is no longer emitted from session resolution but is still emitted from rule-driven `_modified_input`. Claude Code's transparent rewrite for legitimate workflow rules keeps working.

6. Document in the closing commit body: the Codex CLI version (`codex --version`), the Claude Code version (`claude --version`), the exact `mcp__gobby__call_tool` payload that hit the proxy (taken from the daemon log or the direct curl probe), and the absence of retry-message phrases in both transcripts.

Validation criteria: a Codex session and a Claude Code session both succeed first-try at fetching a skill via the canonical SessionStart directive. The user's exact failing trace shape (call_tool with inner `session_id` in `arguments`) — exercised either by Codex naturally or by the direct curl probe in step 3 — produces no retry-message systemMessage and no Codex-side blocking. The targeted test suite from step 1 passes locally.

## Task Mapping

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
