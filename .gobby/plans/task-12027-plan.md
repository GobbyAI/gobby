# Web chat parity: synthesize MCP tool hook events for CodexWebChatBackend

> Canonical plan artifact for task **#12027**. After approval this document is mirrored to `.gobby/plans/task-12027-plan.md` for `/gobby expand` consumption.

## Overview

Web-chat Codex sessions (via the Codex app-server) currently miss end-to-end firing of bundled rules that gate on MCP tool events — including `inject-task-creation-on-schema`, `inject-transition-skill`, and `track-schema-lookup`. Terminal Codex handles these correctly through `SessionMessageProcessor` (rollout tailing), but the web-chat backend has a diverged code path that drops JSON-string tool arguments and has no BEFORE_TOOL synthesis at all. This plan restores full parity: BEFORE_TOOL fires on `item/started` (status=`inProgress`), AFTER_TOOL fires on `item/completed` (status=`completed`), and both share one normalization helper with the terminal adapter so there is one source of truth.

The Codex app-server protocol is authoritative here — its README documents `item/started` and `item/completed` as bracketing notifications around every tool invocation, including `mcpToolCall` items (verified via Context7 `/openai/codex`). No rollout-file tailing is needed; both halves come from the live JSON-RPC notification stream that `CodexAppServerClient` already dispatches.

## Constraints

- **Parity means parity.** BEFORE_TOOL + AFTER_TOOL both fire for web-chat Codex MCP calls, matching the terminal hook lifecycle surface that rules like `track-schema-lookup` depend on.
- **Single source of truth for item-normalization.** `CodexAdapter` and `CodexWebChatBackend` must not both carry their own copy of the `item/completed` parsing logic — the divergent copies are the bug.
- **Do not regress the terminal Codex path.** `TestCodexMcpHookSynthesis` (`tests/sessions/test_sessions_processor_unit.py:861`) and all `tests/adapters/test_codex.py` cases stay green.
- **Do not run the full pytest suite** (CLAUDE.md). Run the specific files in the verification section.
- **`provider_backends.py` is ~1429 lines.** CLAUDE.md Rule 2 (files <1000 lines) is violated today. This plan does NOT split the file — a separate strangler-fig decomposition task is filed as a follow-up (see Phase 3.2). Net line change from this plan is negative (~-70 lines).
- **BEFORE_TOOL must fire before the tool executes.** Synthesizing it from `item/started` preserves the "before" semantic so rules that `block` or inject context still work. Synthesizing at `item/completed` time would silently break that contract.

## Phase 1: Shared item-normalization module

**Goal**: Extract the duplicated Codex `item/*` parsing into one stateless helper module so the terminal adapter and web-chat backend share one implementation — and future-you cannot silently fork it again.

### 1.1 Create `item_normalization.py` with shared helpers [category: refactor]

Target: `src/gobby/adapters/codex_impl/item_normalization.py` (new file, ~150 lines)

Extract the following into stateless module-level functions. Source material is `src/gobby/adapters/codex_impl/adapter.py` at the cited line ranges — verbatim except where noted.

```python
"""Codex item/* notification normalization.

Pure functions that turn raw Codex ThreadItem payloads (as they appear in
item/started and item/completed notifications) into normalized hook event
data. Used by both CodexAdapter (CLI terminal path) and CodexWebChatBackend
(app-server web-chat path) so the two stay bit-for-bit consistent.
"""

from __future__ import annotations

from typing import Any

from gobby.hooks.normalization import normalize_tool_fields

TOOL_ITEM_TYPES: frozenset[str] = frozenset({"commandExecution", "fileChange", "mcpToolCall"})

TOOLISH_FIELDS: frozenset[str] = frozenset({
    "type", "itemType", "name", "toolName", "tool_name",
    "arguments", "toolArgs", "tool_input", "input",
    "output", "result", "toolResult",
    "callId", "call_id", "toolUseId", "tool_use_id",
})


def compose_mcp_tool_name(server: str, tool: str) -> str:
    """Canonical MCP tool-name form used across adapters."""
    return f"mcp__{server}__{tool}"


def extract_completed_item_payload(params: dict[str, Any]) -> dict[str, Any]:
    """Return the tool item payload from an item/started or item/completed params dict.

    Handles both the nested ({params.item: {...}}) and the flat ({params: {...toolish...}})
    shapes. Returns {} if params doesn't look like a tool payload.

    Lifted verbatim from CodexAdapter._extract_completed_item_payload (adapter.py:~200).
    """
    item = params.get("item")
    if isinstance(item, dict):
        return item
    if any(field in params for field in TOOLISH_FIELDS):
        return params
    return {}


def looks_like_tool_item(item: dict[str, Any]) -> bool:
    """Identify Codex items that represent tool execution.

    Lifted verbatim from CodexAdapter._looks_like_tool_item (adapter.py:237-262).
    Critically includes callId/call_id/toolUseId/tool_use_id detection that the
    current web-chat copy (provider_backends.py:1079) is missing.
    """
    item_type = item.get("type") or item.get("itemType")
    if item_type in TOOL_ITEM_TYPES:
        return True
    if any(isinstance(item.get(t), dict) for t in TOOL_ITEM_TYPES):
        return True
    return any(field in item for field in TOOLISH_FIELDS)


def build_tool_event_data(
    item: dict[str, Any],
    *,
    tool_name_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Normalize a Codex tool item (from item/started OR item/completed) into hook event data.

    Returns a dict with at least: tool_name, tool_input. For completed items, also
    tool_response. Always runs through normalize_tool_fields() so mcp_server /
    mcp_tool / tool_output are filled and JSON-string arguments become a dict.

    The tool_name_map parameter is threaded through for CodexAdapter.TOOL_MAP
    canonicalization (read_file → Read, run_shell_command → Bash, etc.). Pass
    None (default) to skip mapping.

    Source: merge of CodexAdapter._build_completed_tool_data (adapter.py:264-314)
    with the only change being input key: for item/started the relevant raw
    fields are arguments and input (no output/result yet), for item/completed
    they are all present.
    """
    item_type = item.get("type") or item.get("itemType") or ""
    nested_payload = item.get(item_type)

    item_data: dict[str, Any] = {}
    if isinstance(nested_payload, dict):
        item_data.update(nested_payload)
    item_data.update(item)

    item_id = item_data.get("id") or item_data.get("itemId") or ""
    raw_tool_name = (
        item_data.get("tool_name") or item_data.get("toolName") or item_data.get("name")
    )
    if not raw_tool_name and item_type == "mcpToolCall":
        server = item_data.get("server") or item_data.get("serverName")
        mcp_tool = item_data.get("tool") or item_data.get("toolName") or item_data.get("name")
        if isinstance(server, str) and server and isinstance(mcp_tool, str) and mcp_tool:
            raw_tool_name = compose_mcp_tool_name(server, mcp_tool)

    if isinstance(raw_tool_name, str) and raw_tool_name:
        mapped = tool_name_map.get(raw_tool_name, raw_tool_name) if tool_name_map else raw_tool_name
        item_data.setdefault("tool_name", mapped)
    elif item_type == "commandExecution":
        item_data.setdefault("tool_name", "Bash")
    elif item_type == "fileChange":
        item_data.setdefault("tool_name", "Write")

    # Move arguments → toolArgs so normalize_tool_fields can JSON-parse it if needed.
    if "tool_input" not in item_data:
        if "arguments" in item_data and "toolArgs" not in item_data:
            item_data["toolArgs"] = item_data["arguments"]
        elif "input" in item_data:
            item_data["tool_input"] = item_data["input"]

    # For completed items only — started items don't have output yet.
    if "tool_response" not in item_data and "tool_result" not in item_data:
        if "output" in item_data:
            item_data["tool_response"] = item_data["output"]
        elif "result" in item_data:
            item_data["tool_response"] = item_data["result"]

    item_data.setdefault("item_id", item_id)
    item_data.setdefault("item_type", item_type)
    item_data.setdefault("status", item.get("status", item_data.get("status", "")))

    normalize_tool_fields(item_data)
    return item_data


def build_pre_tool_lifecycle_payload(
    params: dict[str, Any],
    *,
    tool_name_map: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Extract (tool_name, tool_input) from an item/started notification's params.

    Returns None if the item is not a tool, is a contextCompaction, or has no
    resolvable tool_name. Otherwise returns the two fields needed by
    _apply_pre_tool_lifecycle(tool_name, tool_input).
    """
    item = extract_completed_item_payload(params)
    if not item or not looks_like_tool_item(item):
        return None
    item_type = item.get("type") or item.get("itemType") or ""
    if item_type == "contextCompaction":
        return None
    data = build_tool_event_data(item, tool_name_map=tool_name_map)
    tool_name = data.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    return tool_name, tool_input


def build_post_tool_lifecycle_payload(
    params: dict[str, Any],
    *,
    tool_name_map: dict[str, str] | None = None,
) -> tuple[str, dict[str, Any], Any] | None:
    """Extract (tool_name, tool_input, tool_response) from an item/completed notification's params.

    Returns None for non-tool items or contextCompaction (which maps to PRE_COMPACT
    separately, not AFTER_TOOL — mirrors adapter.py:700 carve-out).
    """
    item = extract_completed_item_payload(params)
    if not item or not looks_like_tool_item(item):
        return None
    item_type = item.get("type") or item.get("itemType") or ""
    if item_type == "contextCompaction":
        return None
    data = build_tool_event_data(item, tool_name_map=tool_name_map)
    tool_name = data.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    return tool_name, tool_input, data.get("tool_response")


def parse_mcp_arguments(raw: Any) -> dict[str, Any]:
    """Parse an MCP elicitation/request arguments value (dict or JSON string) into a dict.

    Used by CodexWebChatBackend's approval-path translator to cover the same
    JSON-string bug that item/completed had. See normalize_tool_fields
    (normalization.py:506-513) for the reference JSON-parse pattern.
    """
    import json as _json
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    return {}
```

Acceptance:
- New file exists with exactly the functions listed above.
- All functions have type hints and one-line docstrings minimum.
- No behavior change when imported but not called (the diff lands pure code; downstream patches in 1.2 and 2.* wire it up).

### 1.2 Delegate `CodexAdapter` normalization methods to the shared module [category: refactor] (depends: 1.1)

Target: `src/gobby/adapters/codex_impl/adapter.py`

Replace the three diverged method bodies with one-line delegations so the terminal adapter and web-chat backend share identical behavior. `CodexAdapter`'s public method surface stays intact — existing adapter unit tests import these methods by name and must continue to work.

Edits:

1. Add imports near the top of the module:
   ```python
   from gobby.adapters.codex_impl.item_normalization import (
       TOOL_ITEM_TYPES as _SHARED_TOOL_ITEM_TYPES,
       build_tool_event_data as _shared_build_tool_event_data,
       compose_mcp_tool_name as _shared_compose_mcp_tool_name,
       extract_completed_item_payload as _shared_extract_completed_item_payload,
       looks_like_tool_item as _shared_looks_like_tool_item,
   )
   ```
2. At line 154, replace the `TOOL_ITEM_TYPES = {...}` class attr with `TOOL_ITEM_TYPES = _SHARED_TOOL_ITEM_TYPES` (preserves the attribute for external consumers while pointing at the single source).
3. Replace `_extract_completed_item_payload` (lines ~200-234) with:
   ```python
   @classmethod
   def _extract_completed_item_payload(cls, params: dict[str, Any]) -> dict[str, Any]:
       return _shared_extract_completed_item_payload(params)
   ```
4. Replace `_looks_like_tool_item` (lines 237-262) with:
   ```python
   @classmethod
   def _looks_like_tool_item(cls, item: dict[str, Any]) -> bool:
       return _shared_looks_like_tool_item(item)
   ```
5. Replace `_build_completed_tool_data` body (lines 264-314) with:
   ```python
   def _build_completed_tool_data(self, item: dict[str, Any]) -> dict[str, Any]:
       return _shared_build_tool_event_data(item, tool_name_map=self.TOOL_MAP)
   ```
6. Replace the two call sites of the internal `_compose_mcp_tool_name` (search `self._compose_mcp_tool_name` and `cls._compose_mcp_tool_name`) with calls to the module helper: `_shared_compose_mcp_tool_name(server, tool)`. If the private method is still referenced elsewhere, leave a one-line delegation stub.

Acceptance:
- `tests/adapters/test_codex.py` runs green with **zero changes** — those tests already exercise this path via `translate_to_hook_event`.
- `tests/sessions/test_sessions_processor_unit.py::TestCodexMcpHookSynthesis` runs green.
- `CodexAdapter.TOOL_MAP`, `CodexAdapter.SAFE_MCP_PROXY_TOOLS`, `CodexAdapter.TOOL_ITEM_TYPES` remain importable class attributes.

## Phase 2: Web-chat backend hook synthesis

**Goal**: Wire `CodexWebChatBackend` to the shared helper so MCP tool calls dispatch BEFORE_TOOL on `item/started` and AFTER_TOOL on `item/completed`, with correctly parsed `tool_input` (JSON-string args included) in both. Also fix the same JSON-string bug in the approval-path translator.

### 2.1 Replace web-chat item-normalization duplicates with shared helpers [category: code] (depends: 1.1)

Target: `src/gobby/servers/websocket/chat/provider_backends.py`

The `CodexWebChatBackend` class in this file is the problem surface. Current broken methods: `_extract_tool_args` (lines 1030-1036, drops JSON-string arguments), `_compose_mcp_tool_name` (1038-1041, duplicate of shared), `_extract_completed_item_payload` (1053-1077, duplicate of shared), `_looks_like_tool_item` (1079-1104, missing callId/call_id/toolUseId/tool_use_id detection), `_build_completed_tool_lifecycle_payload` (1106-1154, never calls `normalize_tool_fields`).

Edits:

1. Add imports at the top:
   ```python
   from gobby.adapters.codex_impl.adapter import CodexAdapter
   from gobby.adapters.codex_impl.item_normalization import (
       build_post_tool_lifecycle_payload,
       build_pre_tool_lifecycle_payload,
       parse_mcp_arguments,
   )
   ```
2. Delete `_extract_tool_args`, `_compose_mcp_tool_name`, `_extract_completed_item_payload`, `_looks_like_tool_item`, `_build_completed_tool_lifecycle_payload` from the class. Keep the class structure otherwise intact.
3. At the `item/completed` dispatch site (line 1369), replace the call to `self._build_completed_tool_lifecycle_payload(params)` with:
   ```python
   payload = build_post_tool_lifecycle_payload(params, tool_name_map=CodexAdapter.TOOL_MAP)
   ```
4. In `_translate_approval_request` (around line 1210-1223, the `mcpToolCall` branch), replace the inline `_extract_tool_args(payload)` with `input_data = parse_mcp_arguments(payload.get("arguments") or payload.get("toolArgs") or payload.get("tool_input") or payload.get("input") or {})`. If the existing code passes the full `payload` to `_extract_tool_args`, adapt so all candidate keys (`arguments`, `toolArgs`, `tool_input`, `input`) are checked in priority order and a JSON-string is parsed.
5. Remove any now-unused imports near the top of the file (run `uv run ruff check --fix` to tidy).

Also update the existing permissive fixture in `tests/servers/websocket/chat/test_runtime_manager.py::TestCodexBackend::test_send_message_applies_post_tool_lifecycle_for_completed_items` (line 551) to the realistic app-server shape so it stops masking the bug:

```python
handler(
    "item/completed",
    {
        "threadId": "thread-1",
        "item": {
            "id": "item-mcp-1",
            "status": "completed",
            "name": "mcp__gobby-tasks__close_task",
            "arguments": json.dumps({"task_id": "#42"}),
            "output": {"success": True},
        },
    },
)
```

Existing assertions on `tool_name="mcp__gobby-tasks__close_task"`, `tool_input={"task_id": "#42"}`, `tool_response={"success": True}` stay unchanged. Before the code fix, the updated fixture fails (empty tool_input); after the fix, it passes.

Expected behavior change:
- Realistic Codex `item/completed` payloads (item has `name="mcp__gobby__get_tool_schema"`, `arguments=json.dumps({...})`, `output={...}`, no `type` field) now produce `tool_input == {"server_name": "gobby-tasks", "tool_name": "create_task"}` and `mcp_server == "gobby"`, `mcp_tool == "get_tool_schema"` — the contract `inject-task-creation-on-schema` checks.

Validation criteria:
- Running `tests/servers/websocket/chat/test_runtime_manager.py::TestCodexBackend::test_send_message_applies_post_tool_lifecycle_for_completed_items` with the realistic fixture passes.
- The TDD sandwich's [TEST] wrapper additionally covers: (a) realistic `item/completed` payload → correct `tool_input` (`server_name`/`tool_name` parsed from JSON string); (b) `mcp_server`/`mcp_tool` derivation; (c) `contextCompaction` items are skipped; (d) non-tool message items are skipped; (e) thread-id mismatch filters the event out.
- `uv run ruff check src/gobby/servers/websocket/chat/` and `uv run mypy src/gobby/servers/websocket/chat/` clean (no new errors beyond pre-existing).

### 2.2 Register `item/started` handler and dispatch BEFORE_TOOL [category: code] (depends: 2.1)

Target: `src/gobby/servers/websocket/chat/provider_backends.py` — specifically `CodexWebChatBackend.send_message._stream_turn` (starts around line 1291).

Per the Codex app-server protocol (verified via Context7 `/openai/codex` — see README `Lifecycle Overview > Event Streaming` and `Dynamic tool calls (experimental) > Tool Invocation Flow`), `item/started` is emitted with status=`inProgress` before any tool item executes, and `item/completed` is emitted after with status=`completed`. Both carry the same `ThreadItem` with a stable `id`, which is how we pair them up.

The current event-method registration list at lines 1334-1341 does NOT include `"item/started"` — BEFORE_TOOL is missing entirely for auto-trusted MCP tool calls.

Edits:

1. In the `event_methods` list inside `_stream_turn` (lines 1334-1341), add `"item/started"` alongside `"item/completed"`.
2. In the event-dispatch loop (around line 1368 after the `if method == "item/completed":` branch), add a new branch **before** the `item/completed` handler:
   ```python
   if method == "item/started":
       pre = build_pre_tool_lifecycle_payload(params, tool_name_map=CodexAdapter.TOOL_MAP)
       if pre is not None:
           tool_name, tool_input = pre
           # Canonical item/started shape: nested params.item.id.
           # Top-level params.itemId is a defensive fallback only.
           nested = params.get("item") if isinstance(params.get("item"), dict) else None
           nested_candidate = nested.get("id") if nested else None
           dedup_key: str | None = None
           if isinstance(nested_candidate, str) and nested_candidate:
               dedup_key = nested_candidate
           else:
               fallback = params.get("itemId")
               if isinstance(fallback, str) and fallback:
                   dedup_key = fallback
           await session._dispatch_before_tool_once(tool_name, tool_input, dedup_key=dedup_key)
       continue
   ```
3. Also update `handle_approval_request` at line 1248 to route through the same helper. Key extraction precedence matches terminal adapter wire-reads:
   ```python
   dedup_key: str | None = None
   if method == "mcpServer/elicitation/request":
       raw = params.get("elicitationId")
       if isinstance(raw, str) and raw:
           dedup_key = raw
   else:
       raw = params.get("itemId")
       if isinstance(raw, str) and raw:
           dedup_key = raw
       else:
           item_type = method.removeprefix("item/").removesuffix("/requestApproval")
           nested = params.get(item_type) or params.get("item") or {}
           if isinstance(nested, dict):
               candidate = nested.get("itemId") or nested.get("id")
               if isinstance(candidate, str) and candidate:
                   dedup_key = candidate
   lifecycle_response = await session._dispatch_before_tool_once(
       tool_name, input_data, dedup_key=dedup_key
   )
   ```
4. Reset `session._before_tool_dedup_keys.clear()` at the top of `_stream_turn` (before `start_turn`) and on receipt of `turn/started`.

Behavior added:
- For every tool item (mcpToolCall, commandExecution, fileChange), `_apply_pre_tool_lifecycle(tool_name, tool_input)` is awaited — which flows through `_on_pre_tool` → `_fire_lifecycle(BEFORE_TOOL)` (wired at `_session.py:401`) → `workflow_handler.evaluate`. Rules keyed on `before_tool` now see the event.
- If a rule returns `decision=block`, `_apply_pre_tool_lifecycle` queues the returned context (via `_queue_deferred_context` at `provider_backends.py:297`) but does NOT prevent Codex from running the tool — the app-server has already started the item by the time we receive `item/started`. This matches the terminal-adapter semantic (Codex on CLI has the same constraint) and is the correct contract. Rules that need to actually block must rely on the approval elicitation path (`handle_approval_request`), which fires synchronously before Codex executes. Documenting this as an explicit non-goal below.
- Approval-path pre-tool dispatch at `handle_approval_request` (line 1248) remains as a synchronous blocking opportunity. `item/started`-based pre-tool is for rules that only need *observation* and *context injection*, not *blocking*.

Note on double-fire avoidance (symmetric, id-based):

Per the Codex app-server protocol (verified: README documents `item/started → item/*/requestApproval → item/completed` for command/file approvals), **`item/started` arrives before any approval request for the same tool item** for command and file paths. The protocol does NOT explicitly document the pairing relationship between `mcpServer/elicitation/request` and any `item/started` for the same logical tool call — that is left as an open runtime question resolved in Phase 3.1 scenario #5.

Design — symmetric dedup keyed on the **canonical stable id** for each event shape, matching the terminal adapter's wire-format reads:

1. `CodexManagedChatSession` gains `_before_tool_dedup_keys: set[str] = field(default_factory=set, repr=False)`, cleared at `turn/started` receipt and at the top of `_stream_turn` (before `start_turn`). The set is bounded by a single turn's tool count.
2. Add helper on the session:
   ```python
   async def _dispatch_before_tool_once(
       self,
       tool_name: str,
       tool_input: dict[str, Any],
       *,
       dedup_key: str | None,
   ) -> dict[str, Any] | None:
       if dedup_key and dedup_key in self._before_tool_dedup_keys:
           return None
       if dedup_key:
           self._before_tool_dedup_keys.add(dedup_key)
       return await self._apply_pre_tool_lifecycle(tool_name, tool_input)
   ```
3. **Both dispatch sites call `_dispatch_before_tool_once`**. Keys are extracted per wire format — this is the authoritative spec, superseding any earlier text in the plan:
   - **`item/started` handler**: the documented wire shape emits the full item under `params["item"]` with `id` nested (`https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md` — item/started carries the full item, and `CodexAppServerClient` forwards notification params verbatim, `src/gobby/adapters/codex_impl/client.py:603`). **Read nested `params["item"]["id"]` first**, with top-level `params["itemId"]` as a defensive fallback only if Codex ever changes its shape. Test fixtures for this event MUST use the nested shape.
   - **`handle_approval_request` for `item/{commandExecution,fileChange,mcpToolCall}/requestApproval`**: `dedup_key = params.get("itemId")` first (matches the terminal adapter's read at `src/gobby/adapters/codex_impl/adapter.py:535` and the adapter-test fixtures at `tests/adapters/test_codex.py:1374` and `:2480`), fallback to nested `params[item_type].get("itemId") or params.get("item", {}).get("id")` only when the top-level is missing.
   - **`handle_approval_request` for `mcpServer/elicitation/request`**: `dedup_key = params.get("elicitationId")` (matches `src/gobby/adapters/codex_impl/adapter.py:497`). `elicitationId` is a stable identifier for the elicitation event itself; it is NOT guaranteed to equal the `itemId` of any paired `item/started`. Cross-id dedup between elicitation and item/started is intentionally NOT attempted here — it would require a verified id-relationship the docs don't promise.
4. **Do NOT fall back to tool-name-based dedup** — two same-name MCP calls in one turn (e.g., two `get_tool_schema` calls with different inner `server_name`/`tool_name`) are legitimate distinct events. Each gets a distinct `params.item.id` (on item/started), distinct `params.itemId` (on `item/*/requestApproval`), or distinct `params.elicitationId` (on elicitation), so keyed dedup preserves them correctly.
5. **The bundled rules this task targets are idempotent by construction** (`inject-task-creation-on-schema.yaml:14`, `inject-transition-skill.yaml:14`, `track-schema-lookup.yaml:12` — all guard on `'X' not in variables[...]`). So if scenario #5 in Phase 3.1 reveals that Codex DOES emit both `item/started` and `mcpServer/elicitation/request` for the same logical MCP tool call (the current unverified case), the variable-level outcome is unchanged and the cost is at most one redundant `get_skill` `mcp_call` effect. A follow-up task can harden cross-id dedup if the observed behavior warrants it.

Expected call ordering (empirically verified portions only):

| Tool class | Event order | BEFORE_TOOL dispatch count |
| --- | --- | --- |
| commandExecution with approval | `item/started` (nested `params.item.id`) → `item/commandExecution/requestApproval` (top-level `params.itemId`) → `item/completed` | One (dedups on the string key shared between the two different field paths) |
| fileChange with approval | `item/started` (nested) → `item/fileChange/requestApproval` (top-level `params.itemId`) → `item/completed` | One (same) |
| mcpToolCall auto-trusted (no approval) | `item/started` (nested) → `item/completed` | One |
| mcpToolCall requiring elicitation | **Unverified** — protocol docs do not document the pairing; resolved in Phase 3.1 scenario #5 | Unverified — measured in scenario #5 |

Validation criteria (expressed as required [TEST] coverage so the TDD sandwich pins them):

Test fixtures must use the **documented wire shapes**:

- `item/started`: nested item (`{"threadId": ..., "item": {"id": "item-1", "name": "mcp__gobby__get_tool_schema", "arguments": json.dumps({...}), ...}}`). Top-level `itemId` is NOT emitted by the app-server for this notification per the protocol docs and per `CodexAppServerClient` pass-through (`src/gobby/adapters/codex_impl/client.py:603`).
- `item/*/requestApproval`: top-level `itemId` (`{"threadId": ..., "itemId": "item-1", ...}` with approval-specific fields). This matches the terminal adapter's read at `adapter.py:535` and the adapter-test fixtures at `tests/adapters/test_codex.py:1374` and `:2480`.
- `mcpServer/elicitation/request`: top-level `elicitationId` per `adapter.py:497`.

- (a) BEFORE_TOOL fires for `item/started` with a realistic mcpToolCall payload (nested `params.item.name="mcp__gobby__get_tool_schema"`, JSON-string `params.item.arguments`, nested `params.item.id`) — asserts `session._on_pre_tool` awaited with `tool_name="mcp__gobby__get_tool_schema"` and `tool_input={"server_name": "gobby-tasks", "tool_name": "create_task"}`.
- (b) **Command/file approval dedup** (the documented command/file order `item/started → item/commandExecution/requestApproval → item/completed`): inject `item/started` with nested `params.item.id="item-1"`, then `item/commandExecution/requestApproval` with top-level `params.itemId="item-1"` → exactly ONE `_on_pre_tool` call. Pins Codex critique #1's fix while matching both events' documented wire shapes.
- (c) **Defensive ordering independence** (same as b with arrivals swapped): inject the approval request first (top-level `params.itemId="item-1"`), then `item/started` with nested `params.item.id="item-1"` → exactly ONE `_on_pre_tool` call. Guards against an inverse arrival if Codex ever changes ordering. Both events dedup on the same key string even though extracted from different field paths.
- (d) **MCP elicitation pairing is NOT pinned in unit tests.** The protocol docs do not document a pairing relationship between `item/started` and `mcpServer/elicitation/request` for MCP tool calls, nor whether `elicitationId` relates to any `itemId`. Do NOT write a unit test that hardcodes the BEFORE_TOOL dispatch count for an MCP tool call that requires elicitation. The implementation must remain safe under either observed outcome (single- or double-fire) via rule idempotency. The actual wire behavior is measured in Phase 3.1 scenario #5; any follow-up unit-test pinning is a subsequent task, NOT this plan.
- (e) **Distinct MCP calls in one turn are NOT suppressed**: two `item/started` events with different nested `params.item.id` but same `params.item.name="mcp__gobby__get_tool_schema"` (different inner `tool_name` in `params.item.arguments` — e.g., `create_task` vs `claim_task`) → TWO `_on_pre_tool` calls, both with correct distinct `tool_input`. (Guards against Codex critique #2.)
- (f) `item/started` with `item.type="contextCompaction"` or a plain-message item does NOT trigger `_on_pre_tool`.
- (g) `item/started` with mismatched `threadId` is filtered out by `_matches`.
- (h) BEFORE_TOOL fires for `item/started` with `type="commandExecution"` as `tool_name="Bash"` and `type="fileChange"` as `tool_name="Write"`.
- (i) Existing `test_handle_approval_request_respects_managed_pre_tool_block` at `tests/servers/websocket/chat/test_runtime_manager.py:489` still passes — approval path continues to dispatch BEFORE_TOOL synchronously and honor `decision=block`.
- (j) Turn boundaries reset the dedup set: two `item/started` events with the same nested `params.item.id` across two `turn/started` notifications → TWO `_on_pre_tool` calls (dedup is per-turn, not session-lifetime).

Also add the session field + helper to `CodexManagedChatSession` (defined around line 550-620 of `provider_backends.py`):

```python
_before_tool_dedup_keys: set[str] = field(default_factory=set, repr=False)

async def _dispatch_before_tool_once(
    self,
    tool_name: str,
    tool_input: dict[str, Any],
    *,
    dedup_key: str | None,
) -> dict[str, Any] | None:
    """Dispatch BEFORE_TOOL, deduplicating by the wire-format stable id.

    The dedup_key is extracted per event shape, matching the documented
    Codex app-server wire format (NOT a single uniform field):
      - item/started — nested params["item"]["id"] (canonical app-server
        shape; top-level params["itemId"] is a defensive-only fallback)
      - item/{commandExecution,fileChange,mcpToolCall}/requestApproval —
        top-level params["itemId"] (matches the terminal adapter at
        src/gobby/adapters/codex_impl/adapter.py:535)
      - mcpServer/elicitation/request — top-level params["elicitationId"]
        (matches adapter.py:497)

    itemId and elicitationId are independent id spaces; cross-id dedup
    between elicitation and item/started is intentionally not attempted
    — that requires a protocol-verified relationship the docs do not
    document. Pass dedup_key=None to skip dedup for a specific dispatch
    (e.g., if a future wire format emerges with no stable id).
    """
    if dedup_key and dedup_key in self._before_tool_dedup_keys:
        return None
    if dedup_key:
        self._before_tool_dedup_keys.add(dedup_key)
    return await self._apply_pre_tool_lifecycle(tool_name, tool_input)
```

## Phase 3: Verification & follow-ups

**Goal**: End-to-end prove the rules fire in a live web-chat Codex session via the Chrome DevTools MCP, and file the separate strangler-fig refactor task.

### 3.1 Live end-to-end verification via Chrome DevTools MCP [category: manual] (depends: Phase 2)

Target: running daemon + browser session controlled by the `chrome-devtools` MCP server. No repo files are modified. Scenarios #4 and #5 install an ephemeral diagnostic rule in the **database** via the `create_rule` MCP tool (auto-export to `~/.gobby/workflows/`, outside the tracked source tree) and remove it in cleanup; the tracked source tree is never mutated.

Context — actual ports (from `src/gobby/install/shared/config/bootstrap.yaml`):

| Purpose | Port | URL |
| --- | --- | --- |
| Gobby HTTP (REST API) | 60887 | `http://localhost:60887` |
| Gobby WebSocket (chat stream) | 60888 | `ws://localhost:60888` |
| Web UI (dev server) | 60889 | `http://localhost:60889` ← this is the one the browser opens |

Correlation strategy — since `_fire_lifecycle`'s debug log at `src/gobby/servers/websocket/chat/_lifecycle.py:134` does NOT include `conversation_id` or `db_session_id`, log-filtering by conversation is infeasible without a logging change. Rule fires are NOT persisted to a dedicated audit table either — only their variable side-effects land in `session_variables` (per `src/gobby/workflows/CLAUDE.md` — "workflow event history" is not a real surface). Use these three real observables:

1. **Variable inspection via the Gobby MCP proxy (authoritative, persisted)**: call `mcp__gobby__get_variable(name="injected_skills", session_id=<db_session_id>)` and `mcp__gobby__get_variable(name="unlocked_tools", session_id=<db_session_id>)`. **Do NOT pass the frontend `conversation_id`** — web chat rewrites the DB row's `external_id` to the provider SDK session id during runtime (`src/gobby/servers/websocket/chat/_messaging.py:706` and `src/gobby/servers/websocket/chat/_session.py:758`), so a `conversation_id` that resolved on turn 1 can fail to resolve on later turns via the session-reference resolver at `src/gobby/mcp_proxy/tools/workflows/_variables.py:174` / `src/gobby/storage/session_resolution.py:19`. Use the `db_session_id` (a UUID primary key on the `sessions` table) — it is globally stable and unambiguous. **Do NOT use the `session_ref` `#N` format here either**: `seq_num` is unique only on `(project_id, seq_num)` (see `src/gobby/storage/baseline_schema.sql:230`), so ref-style lookups must be project-scoped by the resolver; using the UUID avoids the whole class of scoping bugs. Web chat emits `db_session_id` on the `session_info` WebSocket frame (`src/gobby/servers/websocket/chat/_messaging.py:420-430`) — capture it there and use it for every MCP `get_variable` call in the verification run. `get_variable` is a top-level tool on the `gobby` MCP server (defined at `src/gobby/mcp_proxy/server.py:468`, registered at `src/gobby/mcp_proxy/server.py:525`); there is no separate "gobby-variables" server — the `gobby-variables` token appears only as a suggestion alias in `src/gobby/mcp_proxy/services/tool_proxy.py:42` and must NOT be used as a literal `server_name`. These calls read the `session_variables` table rows that rules' `set_variable` effects write. Presence or absence of specific values in these lists is the ground-truth proof that a rule fired.
2. **Chrome DevTools MCP — WebSocket frame inspection**: open the Network panel, filter for the `ws://localhost:60888` connection, capture the sequence of JSON-RPC notifications during the turn. Confirm `item/started` (status=`inProgress`) and `item/completed` (status=`completed`) frames both arrive for the `get_tool_schema` call. Record the top-level `itemId` (or, if only nested, the `item.id`) value to verify dedup.
3. **SQLite inspection (direct, when the MCP path is unavailable)**: the `session_variables` table stores ONE row per session with a JSON blob in the single `variables` column — NOT a `name, value` pair-per-row table (schema: `src/gobby/storage/baseline_schema.sql:438-442`, loader: `src/gobby/workflows/state_manager.py:147`). Query by `db_session_id` UUID (captured from the `session_info` frame) and parse the JSON client-side:

    ```bash
    sqlite3 ~/.gobby/gobby-hub.db "SELECT variables FROM session_variables WHERE session_id = '<db_session_id>';"
    ```

    Pipe through `jq '.injected_skills'` / `jq '.unlocked_tools'` to extract the specific keys. Do NOT attempt `seq_num`-based lookups here — `seq_num` is unique per `(project_id, seq_num)` (`baseline_schema.sql:230`), so a bare `WHERE s.seq_num = <N>` can hit the wrong row across projects. Do NOT use `gobby sessions show` as an audit source — per `src/gobby/cli/sessions.py:111` it emits session metadata/summary only, not a rule-fire stream.

Steps:

1. `uv run gobby restart`. Wait for `gobby status` to report all services healthy. If chrome-devtools MCP is not yet loaded, load it: `add_mcp_server` with the canonical chrome-devtools spec.
2. Navigate the controlled Chrome tab to `http://localhost:60889/` — the web-chat UI.
3. In the UI, select the **Codex** provider, start a new conversation. The backend emits a `session_info` WebSocket frame (`src/gobby/servers/websocket/chat/_messaging.py:420-430`) as part of session bootstrap — capture the `db_session_id` UUID field via the chrome-devtools Network panel. This UUID is the durable Gobby-side handle used for all subsequent MCP `get_variable` calls and all SQLite fallback queries in this verification. The `session_ref` (`#N`) field on the `done` frame is useful for human-readable logs but should NOT be passed as `session_id` — `seq_num` is project-scoped, so UUID is strictly better. The UI-level `conversation_id` is ALSO recorded (useful for WebSocket filtering) but is NOT used as the `session_id` argument to `get_variable`.
4. Send prompt: *"Create a gobby task titled 'verify web chat hook dispatch'."*
5. Observe the assistant call `get_tool_schema(server_name="gobby-tasks", tool_name="create_task")`:
   - In the UI, a tool-call event surfaces.
   - In the DevTools Network tab on the `:60888` WebSocket, capture the `item/started` frame (method=`item/started`, params.item.name=`mcp__gobby__get_tool_schema`) AND the `item/completed` frame with matching `item.id`.
6. Assertions (scenario #1 — task-creation inject):
   - **MCP variable**: `mcp__gobby__get_variable(name="injected_skills", session_id=<db_session_id>)` returns a list containing `"task-creation"`.
   - **WebSocket frames**: both `item/started` (status=`inProgress`) and `item/completed` (status=`completed`) for the `get_tool_schema` call are captured; same `itemId` in both (the id observation is the evidence that dedup has a stable anchor; it is not an assertion about rule-fire count here).
   - **Unlock tracking**: `mcp__gobby__get_variable(name="unlocked_tools", session_id=<db_session_id>)` contains `"gobby-tasks:create_task"` (proves `track-schema-lookup` also fired).
7. Screenshot the DevTools Network panel showing the two frames. Save to the task artifact directory.
8. Scenario #2 — task-transitions inject: in the same conversation, prompt *"Close that task for me."* Assistant fetches `get_tool_schema` for `close_task` (or similar lifecycle tool). Assert `injected_skills` now contains `"task-transitions"`.
9. Scenario #3 — non-matching schema (negative case): in a FRESH conversation (important — avoids the rule's guard short-circuiting in the already-injected variable state), prompt *"Show me the schema for list_tools."* Assertion: `mcp__gobby__get_variable(name="injected_skills", session_id=<new_db_session_id>)` returns an empty list or a list that does NOT contain `"task-creation"` or `"task-transitions"`.
10. Scenario #4 — dedup verification (command/file path):

    Install a diagnostic rule AT RUNTIME via the Gobby MCP proxy — no repo mutation and no daemon restart required. The `create_rule` tool lives in the **internal `gobby-workflows` tool registry** (`src/gobby/mcp_proxy/tools/workflows/__init__.py:112` registers the registry, `:341` exposes `create_rule`). It is **NOT** a top-level `gobby` tool. Use progressive discovery first, then `call_tool` with `server_name="gobby-workflows"`. The exposed parameter is `make_template`, not `make_global_template` — the wrapper at `tools/workflows/__init__.py:348-355` maps `make_template → make_global_template` when calling the inner implementation.

    Progressive discovery sequence — per `AGENTS.md:24`, the required order is `list_mcp_servers → list_tools → get_tool_schema → call_tool`. Start at the top even if `gobby-workflows` is already known, so the artifact matches the enforced workflow:

    ```python
    mcp__gobby__list_mcp_servers()
    mcp__gobby__list_tools(server_name="gobby-workflows")
    mcp__gobby__get_tool_schema(server_name="gobby-workflows", tool_name="create_rule")
    mcp__gobby__get_tool_schema(server_name="gobby-workflows", tool_name="delete_rule")
    mcp__gobby__get_tool_schema(server_name="gobby-workflows", tool_name="list_rules")
    ```

    Then install:

    ```python
    mcp__gobby__call_tool(
      server_name="gobby-workflows",
      tool_name="create_rule",
      arguments={
        "name": "_dedup_probe_task_12027",
        "definition": {
          "event": "before_tool",
          "enabled": True,
          "priority": 999,
          "when": "True",
          "effects": [
            {
              "type": "set_variable",
              "variable": "_dedup_probe",
              "value": "variables.get('_dedup_probe', 0) + 1"
            }
          ],
          "tags": ["user", "diagnostic", "ephemeral"]
        },
        "make_template": True
      }
    )
    ```

    Then open a fresh web-chat Codex conversation. Prompt: *"Write 'hello' to /tmp/hook-parity-test.txt."* The agent issues a `Write` tool call. Capture the WebSocket frames. Expected sequence: `item/started` (params.item.id=X) → `item/fileChange/requestApproval` (top-level params.itemId=X) → approve in UI → `item/completed` (params.item.id=X).

    Assertion: `mcp__gobby__get_variable(name="_dedup_probe", session_id=<db_session_id>)` equals exactly **1**. If >1, Phase 2.2's dedup-key extraction is broken — the same item fired BEFORE_TOOL twice.

    Cleanup (REQUIRED): after scenario #4 + scenario #5 both run, call:

    ```python
    mcp__gobby__call_tool(
      server_name="gobby-workflows",
      tool_name="delete_rule",
      arguments={"name": "_dedup_probe_task_12027", "force": True}
    )
    ```

    Also delete the auto-exported YAML if it lingers: `rm -f ~/.gobby/workflows/rules/_dedup_probe_task_12027.yaml`. Verify removal with `mcp__gobby__call_tool(server_name="gobby-workflows", tool_name="list_rules", arguments={})` — no `_dedup_probe_task_12027` entry should appear.

11. Scenario #5 — MCP elicitation double-fire observation (behavior-to-verify, not a pin): in a fresh conversation, trigger an MCP tool call that DOES elicit approval (e.g., configure a non-safe MCP tool invocation that doesn't match `SAFE_MCP_PROXY_TOOLS`). Capture the WebSocket frames. Record: (a) whether `item/started` fires for the mcpToolCall before the elicitation, (b) the `itemId` on item/started, (c) the `elicitationId` on the elicitation request, (d) whether they share any stable relationship. Using the same `_dedup_probe` rule from scenario #4, record the fire count. The report captures whichever outcome is observed (1 or 2 fires); if 2 fires, note it explicitly and file a follow-up task to establish cross-id dedup if warranted.

Output — a markdown report with:
- Per-scenario pass/fail outcome (scenarios #1–#4) or observed outcome (scenario #5).
- The DevTools Network screenshot.
- The `mcp__gobby__get_variable` responses for each scenario (`injected_skills`, `unlocked_tools`, `_dedup_probe` where applicable).
- The captured `itemId` / `elicitationId` values for scenarios #4 and #5.
- Confirmation that the `_dedup_probe_task_12027` rule and its auto-exported YAML at `~/.gobby/workflows/` were removed during cleanup.

Attach to task #12027 as validation evidence.

If any scenario fails, escalate back to Phase 2.

Acceptance:
- Scenarios #1, #2, #3 pass.
- Scenario #4: `_dedup_probe` == 1 after the Write tool approves. If >1, Phase 2.2's `itemId` extraction is broken — loop back.
- Scenario #5 outcome recorded verbatim (single-fire or double-fire); if double-fire, a follow-up task is filed referencing the observed `itemId`/`elicitationId` relationship.
- Artifact (markdown report + screenshot) attached to #12027.

### 3.2 File strangler-fig decomposition task for provider_backends.py [category: planning]

Target: new Gobby task (separate from #12027).

Two different plan-mode semantics are in play and both matter:
- **Gobby plan-mode** (session variable, per `AGENTS.md:35` / `CLAUDE.md`) allows `gobby-tasks` MCP calls during planning. This is the rule Codex critique #4 cited.
- **Claude Code `EnterPlanMode`** (native harness mode) is strictly read-only — all non-read-only tool calls are blocked by the harness hook, including `create_task`. This overrides Gobby's permissiveness while the native plan mode is active.

Because this plan workflow runs inside Claude Code's native `EnterPlanMode`, task creation **must be executed immediately after `ExitPlanMode`** (the harness unblocks non-read-only calls on exit), NOT deferred and not attempted during drafting. The calling agent/human is responsible for invoking `create_task` with the fields below as the first post-approval action.

Call `create_task` via the gobby-tasks MCP with these fields:

- **Title**: `Refactor: strangler-fig decomposition of provider_backends.py (~1429 lines, violates Rule 2)`
- **Type**: `refactor`
- **Category**: `code`
- **Priority**: 3
- **Labels**: `refactor`, `web-chat`, `codex`, `tech-debt`, `strangler-fig`
- **Description**: `src/gobby/servers/websocket/chat/provider_backends.py` hosts three provider backends (Codex, Gemini, Qwen), the `ManagedChatSessionBase`, and permission mixins in one ~1429-line module — violates CLAUDE.md Rule 2 (<1000 lines). Decompose via strangler-fig: carve each backend + its managed-session class into `src/gobby/servers/websocket/chat/backends/{codex,gemini,qwen}.py`, extract `ManagedChatSessionBase` + `ProviderBackendHealth` into `base.py`, keep `provider_backends.py` as a re-export shim until all external imports migrate. No behavior change; no test changes beyond import-path updates.
- **Validation criteria**: each new module <600 lines; `provider_backends.py` deleted or <200 lines of pure re-exports; `uv run ruff check src/` and `uv run mypy src/gobby/servers/websocket/chat/` clean; `runtime_manager` + `provider_routing` tests pass unchanged.
- **Not a blocker** for #12027 — this plan's edits are additive-and-deletion on the existing file; the strangler-fig work can happen independently afterwards.

Acceptance:
- Task created via `create_task` as the first action after `ExitPlanMode` is approved (not before — the native harness blocks it during plan mode).
- Returned `seq_num` recorded in the Task Mapping table below.
- Reference added to #12027 as a comment: "Follow-up: #<new_seq> — decompose provider_backends.py".

## Task Mapping

<!-- Filled in by /gobby expand after task tree is built -->

| Plan Item | Task Ref | Status |
|-----------|----------|--------|
| Root | #12027 (existing) | open |
| Phase 1 | | |
| 1.1 | | |
| 1.2 | | |
| Phase 2 | | |
| 2.1 | | |
| 2.2 | | |
| Phase 3 | | |
| 3.1 | | |
| 3.2 | | |

## Risks (for adversary review)

Resolved by revision (keeping these so the adversary can audit that the fixes are real):

- **[Resolved — Codex critique 1a/#1]** Symmetric dedup: `item/started` arrives BEFORE `requestApproval` per protocol. Plan now dedups on `itemId` at both dispatch sites via `_dispatch_before_tool_once`. See Phase 2.2.
- **[Resolved — Codex critique 2a/#2]** Tool-name fallback dedup is removed. For MCP elicitations, `elicitationId` is used as the dedup key (matching terminal adapter at `adapter.py:497`); same-name MCP calls with different `tool_input` always get distinct `itemId` / `elicitationId` values. Scenario (e) in 2.2's validation matrix proves they both fire.
- **[Resolved — Codex critique 3a/#3]** Correct ports in Phase 3.1 (UI at `:60889`, WebSocket at `:60888`). Correlation via `mcp__gobby__get_variable` (Gobby MCP proxy, top-level tool on the `gobby` server — `src/gobby/mcp_proxy/server.py:468`) + chrome-devtools Network frames + direct SQLite inspection. `gobby sessions show` removed from the audit path (it doesn't expose rule fires).
- **[Partially resolved — Codex critique 4a/#4]** Two plan-mode semantics exist (Gobby plan-mode variable allows task MCP per `AGENTS.md:35`; Claude Code native `EnterPlanMode` is strictly read-only at harness level). This plan runs inside native plan mode, which blocks `create_task`. Phase 3.2 sequences task creation as first post-`ExitPlanMode` action.
- **[Resolved — Codex critique 5a/#5]** Removed `test_item_normalization.py` from the automated verification list; TDD-sandwich tests on 2.1/2.2 cover the helper transitively.

Round-6 critiques resolved:

- **[Resolved — Codex critique 1f]** Phase 3.1's SQLite fallback query was wrong — `session_variables` is one row per session with a single JSON `variables` column (`src/gobby/storage/baseline_schema.sql:438-442`, loaded by `src/gobby/workflows/state_manager.py:147`), not a `name, value` per-row table. Rewritten to `SELECT variables FROM session_variables WHERE session_id = '<db_session_id>';` and directs the validator to parse the JSON blob client-side with `jq`.
- **[Resolved — Codex critique 2f]** Switched the primary session handle from `session_ref` (`#N`) to `db_session_id` UUID. `seq_num` is unique only on `(project_id, seq_num)` per `src/gobby/storage/baseline_schema.sql:230`, so ref-style lookups are project-scoped and can hit the wrong row across projects. `db_session_id` is the primary key on `sessions` and is globally unique. Capture from the `session_info` WebSocket frame at `src/gobby/servers/websocket/chat/_messaging.py:420-430`, then use the UUID for every MCP `get_variable` call AND the SQLite fallback query. The round-5 critique (use `session_ref`) was correct to reject `conversation_id`, but `db_session_id` is the stricter correct handle.

Round-5 critiques resolved:

- **[Resolved — Codex critique 1e]** Phase 3.1 was telling the validator to pass the UI-level `conversation_id` as `session_id` to `get_variable`. That fails after the first turn because web chat rewrites the DB row's `external_id` to the provider SDK session id at runtime (`src/gobby/servers/websocket/chat/_messaging.py:706`, `_session.py:758`), while `get_variable` resolves through the normal session-reference resolver (`src/gobby/mcp_proxy/tools/workflows/_variables.py:174`, `src/gobby/storage/session_resolution.py:19`) which looks up by `external_id`. Fixed: the verification steps now capture the durable Gobby-side `session_ref` (format `#N`) from the `done` WebSocket frame's `session_ref` field (computed at `_messaging.py:346`, attached at `:686`) and use it for every MCP `get_variable` call. The SQLite inspection example also switched from `WHERE external_id = '<conv_ref>'` to `WHERE seq_num = <N>` so it tolerates the runtime rewrite.
- **[Resolved — Codex critique 2e]** `_dispatch_before_tool_once` docstring was still asserting "The dedup_key is the top-level itemId (for item/started and item/*/requestApproval events)..." which contradicted the rest of the plan and the validation matrix. Rewritten to enumerate the three event shapes and their canonical extraction paths explicitly: nested `params["item"]["id"]` for item/started, top-level `params["itemId"]` for `item/*/requestApproval`, top-level `params["elicitationId"]` for `mcpServer/elicitation/request`.
- **[Resolved — Codex critique 3e]** Scenario #4's progressive-discovery block skipped `list_mcp_servers`, violating the AGENTS.md:24 required order. Added `mcp__gobby__list_mcp_servers()` as the first step of the sequence so the artifact matches the enforced workflow.

Round-4 critiques resolved:

- **[Resolved — Codex critique 1d]** Phase 3.1 scenario #4's `create_rule` call was wrongly routed to `server_name="gobby"` with param `make_global_template`. Corrected: `create_rule`/`delete_rule`/`list_rules` live in the **internal `gobby-workflows` registry** (`src/gobby/mcp_proxy/tools/workflows/__init__.py:112`, `:341`, `:357`). Plan now requires progressive discovery on `gobby-workflows` first and uses the exposed parameter name `make_template` (which the wrapper at `:348-355` maps to inner `make_global_template`). Same correction for `delete_rule` and `list_rules`.
- **[Resolved — Codex critique 2d]** `item/started` test fixtures previously specified top-level `itemId`, but the app-server docs describe `item/started` as emitting the full item with nested `item.id` as the stable identifier — and `CodexAppServerClient` just forwards notification params (`src/gobby/adapters/codex_impl/client.py:603`). Implementation code keeps a defensive top-level fallback, but the **authoritative extraction path for `item/started` is now nested `params["item"]["id"]`**, and all validation-matrix fixture descriptions explicitly use the nested shape. `item/*/requestApproval` fixtures use top-level `params.itemId` per adapter precedent.
- **[Resolved — Codex critique 3d]** Double-prefix typo `mcp__gobby__mcp__gobby__get_variable` corrected to `mcp__gobby__get_variable` at the Phase 3.1 correlation-strategy line.

Round-3 critiques resolved:

- **[Resolved — Codex critique 1c]** Phase 2.2's "Note on double-fire avoidance" subsection was still asserting the wrong extraction (`params.get("item", {}).get("id")` / `item_id=None` for MCP). Rewritten so the ONE authoritative spec — **nested `params["item"]["id"]` for `item/started`** (documented wire shape), **top-level `params["itemId"]` for `item/*/requestApproval`** (terminal-adapter precedent), **`params["elicitationId"]` for `mcpServer/elicitation/request`** — lives in one place and is repeated consistently across the code snippet, the session helper, and the validation matrix. (Round-3 wording initially called both dispatch sites "top-level itemId"; round-4's critique 2d corrected the `item/started` side to nested `params.item.id`.)
- **[Resolved — Codex critique 2c]** Expected-call-order table's MCP-elicitation row changed from "Two (approval path has no item.id; rule idempotency absorbs redundancy)" to "**Unverified** — protocol docs do not document the pairing; resolved in Phase 3.1 scenario #5". The prose claim "the elicitation arrives in the middle of the item lifecycle in all cases" is deleted. The unit-test matrix's scenario (d) is explicit that MCP elicitation behavior is NOT unit-test pinned.
- **[Resolved — Codex critique 3c]** Phase 3.1 scenario #4's diagnostic rule no longer mutates the repo. Install is via the `create_rule` MCP tool (routed to `server_name="gobby-workflows"` per round-4 critique 1d; exposed param is `make_template=True`, which the wrapper at `src/gobby/mcp_proxy/tools/workflows/__init__.py:348-355` maps to the internal `make_global_template=True`). Auto-export lands in `~/.gobby/workflows/` (user home, NOT the tracked source tree). Cleanup via `delete_rule` on `gobby-workflows` + `rm -f ~/.gobby/workflows/rules/_dedup_probe_task_12027.yaml` is required and listed as an explicit step. Phase 3.1's header also acknowledges the DB-level diagnostic-rule install as an ephemeral mutation of runtime state (not source).
- **[Resolved — Codex critique 4c]** All references to "gobby-variables MCP proxy" replaced with explicit `mcp__gobby__get_variable` calls (server_name="gobby"). Clarified that `gobby-variables` is only an alias in `src/gobby/mcp_proxy/services/tool_proxy.py:42`, not a real `server_name`.

Round-2 critiques resolved:

- **[Resolved — Codex critique 1b]** `itemId` extraction precedence in `handle_approval_request` fixed: top-level `params["itemId"]` first (matches `adapter.py:535`, `tests/adapters/test_codex.py:1374`, `:2480`), nested `params[item_type].itemId` / `params.item.id` as fallback. For `mcpServer/elicitation/request`, use `params["elicitationId"]` (matches `adapter.py:497`).
- **[Resolved — Codex critique 2b]** MCP elicitation double-fire is NO LONGER a pinned unit-test contract. Phase 2.2 scenario (d) is reframed as "behavior-to-verify at runtime in Phase 3.1 scenario #5" — unit tests do not hardcode the fire count. The implementation remains safe under either outcome (single or double fire) because rules are idempotent.
- **[Resolved — Codex critique 3b]** Phase 3.2 contradiction removed — task creation happens as first post-`ExitPlanMode` action; no conflicting "filed during drafting" wording.
- **[Resolved — Codex critique 4b]** `gobby sessions show` removed as an audit source. Replaced with direct `session_variables` observation via `mcp__gobby__get_variable` (the top-level tool on the `gobby` server; `gobby-variables` is only a suggestion alias, not a real server name) or raw SQLite, and with an in-scenario `_dedup_probe` diagnostic rule installed via the `create_rule` MCP tool (not a repo file drop) that increments on every `before_tool` fire so scenario #4 has a real count to assert against.

Open risks for the adversary to confirm or dismiss:

1. **Wire-format drift for itemId / elicitationId.** The extraction precedence now mirrors the terminal adapter exactly (top-level `params["itemId"]` for `item/*/requestApproval`, `params["elicitationId"]` for `mcpServer/elicitation/request`, with nested fallbacks preserved defensively). If Codex introduces a new approval surface that carries the stable id under a new field (e.g., a `guardianAction.id` under a future `item/autoApprovalReview/*` event), our extraction will return None, dedup will be skipped, and BEFORE_TOOL may double-fire. Mitigation: scenario #4's `_dedup_probe` count is the production canary; scenario #5 records `elicitationId` / `itemId` shapes explicitly in the report so regressions surface fast.

2. **`item/started` for commandExecution/fileChange in "plan mode"**. In web-chat "plan" mode, writes are declined at `handle_approval_request` (provider_backends.py:1260-1272). `item/started` still fires the observational BEFORE_TOOL before the decline. Rules that inject context based on a tool that then gets declined might produce misleading context. Verify: is this acceptable for the bundled rules, or do we need a post-decline rollback mechanism? (Initial read: all bundled rules are additive context-injection; no rollback needed.)

3. **`mcp__gobby__call_tool` inner dispatch.** When the agent uses the proxy tool `call_tool` instead of a direct `mcp__gobby__<tool>` invocation, `tool_name` is `mcp__gobby__call_tool` and the actual target is in `tool_input.server_name`/`tool_name`. `normalize_tool_fields` handles this at `normalization.py:611-626` by promoting inner values to `mcp_server`/`mcp_tool`. Question: does the item/started payload for a `call_tool` invocation carry the inner target in `arguments` the same way `item/completed` does, or does Codex emit a *nested* item for the inner call? If the shape differs between started and completed, `build_pre_tool_lifecycle_payload` vs `build_post_tool_lifecycle_payload` would diverge and a single shared helper wouldn't suffice.

4. **Logger destination.** `_fire_lifecycle` logs to `gobby.servers.websocket.chat._lifecycle`; `HookManager.handle` (CLI path) logs to `gobby.hooks` which writes to `~/.gobby/logs/hook-manager.log`. Task #12027's validation criteria mentions hook-manager.log entries. After this plan, web-chat events appear in `~/.gobby/logs/gobby.log` but NOT in `hook-manager.log`. Is unified logging part of parity, or out of scope? Recommend: out of scope for this task; file as a follow-up if unified logging is desired.

5. **Idempotency of `normalize_tool_fields`.** After the fix, `normalize_tool_fields` runs twice on the same dict (once inside `build_tool_event_data`, once in `_fire_lifecycle` at `_lifecycle.py:109`). Most branches use `setdefault`/`not in data` guards, but `tool_name` canonicalization at `normalization.py:503` uses unconditional assignment (`data["tool_name"] = canonicalize_shell_tool_name(data["tool_name"])`). Verify `canonicalize_shell_tool_name` is idempotent (I believe it is — it normalizes case/separators — but worth a direct check via a property-style test: `f(f(x)) == f(x)` for all tool names).

6. **`contextCompaction` PRE_COMPACT gap.** The terminal path routes `contextCompaction` items to PRE_COMPACT synthesis at `adapter.py:700`. The web-chat path has no PRE_COMPACT handling tied to `item/completed`. This is a pre-existing gap — not introduced or expanded by this plan, but worth flagging as a separate follow-up.

## Verification (automated — specific files only)

The shared helper module is covered transitively by the TDD sandwich tests on tasks 2.1 (AFTER_TOOL parity) and 2.2 (BEFORE_TOOL parity) — both exercise the full call path through `build_pre_tool_lifecycle_payload` / `build_post_tool_lifecycle_payload`. No dedicated `test_item_normalization.py` file is required (plan-draft reserves the `test` category for fixtures/infra, not test cases, and no test case here is outside the TDD-sandwich scope). If a future change makes direct unit tests on the helper valuable, add a separate task rather than shoehorning one here.

```bash
uv run pytest tests/adapters/test_codex.py -v
uv run pytest tests/servers/websocket/chat/test_runtime_manager.py -v
uv run pytest tests/sessions/test_sessions_processor_unit.py::TestCodexMcpHookSynthesis -v
uv run pytest tests/workflows/test_codex_skill_injection.py -v
uv run ruff check src/ --fix
uv run mypy src/gobby/adapters/codex_impl/ src/gobby/servers/websocket/chat/
```
