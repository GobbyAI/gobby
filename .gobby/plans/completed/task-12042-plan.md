# Fix test_mcp_steps_use_child_session_id — Wire session_manager on AsyncMock tool_proxy

## Context

The test `tests/workflows/test_pipeline_executor.py::TestPipelineChildSession::test_mcp_steps_use_child_session_id` has been failing since the session-id-resolution refactor (commits #12032 / #12034). That refactor taught `execute_mcp_step` (in `src/gobby/workflows/pipeline/handlers.py`) to reach into `tool_proxy.session_manager` and seed resolved contexts via `resolve_and_seed_contexts` (in `src/gobby/utils/session_context.py`). The test was not updated to match.

The failing test constructs `tool_proxy = AsyncMock()` and never sets `tool_proxy.session_manager`. Accessing it auto-generates another `AsyncMock`. The helper then calls the **synchronous** `session_manager.resolve_session_reference(...)` — which on an `AsyncMock` returns a coroutine object, not a string. `session_context.py:231-233` wraps the result in `str(...)`, producing a `"<coroutine object AsyncMockMixin._execute_mock_call at 0x...>"` repr. That garbage flows into `effective_session_id` and is forwarded to `tool_proxy.get_tool_schema` / `tool_proxy.call_tool` as `session_id=<garbage>`, breaking the `session_id="child-session-mcp"` assertions at lines 2098–2108.

## Overview

This is a **test fixture bug, not a handler bug.** The handler's call chain matches production. Stubbing `resolve_session_reference` on a `MagicMock` wired into `tool_proxy.session_manager` is the established convention across the codebase (e.g. `tests/mcp_proxy/test_mcp_tools_session_messages.py:204-205, :233, :306, :331, :356, :426, :505`). The fix restores coverage of the resolution path that the test already intended to exercise.

## Constraints

- **Do not modify** `src/gobby/workflows/pipeline/handlers.py` or `src/gobby/utils/session_context.py`. Defensive type-checking in the handler (e.g. "fall back to raw `session_ref` if resolver returns non-string") would mask real resolver regressions in production and contradicts CLAUDE.md principle 12 (always choose the most correct fix).
- **Do not** extract a shared `_attach_session_manager` helper or a `tool_proxy` fixture as part of this change. Only one test in `TestPipelineChildSession` exercises `execute_mcp_step`; a helper would be speculative infrastructure. If a second MCP-step test appears later, that is when extraction pays for itself.
- Keep the diff local to the single failing test.

## Phase 1: Wire mock session_manager on tool_proxy

**Goal**: Make `test_mcp_steps_use_child_session_id` pass by attaching an explicit `MagicMock` session_manager to `tool_proxy` and stubbing the methods the handler invokes.

### 1.1 Wire mock_session_manager onto tool_proxy in test_mcp_steps_use_child_session_id [category: refactor]

Target: `tests/workflows/test_pipeline_executor.py` lines 2052–2108 (the body of `test_mcp_steps_use_child_session_id`).

**Current state (lines 2058–2068):**

```python
mock_session_manager = MagicMock()
child_session = MagicMock()
child_session.id = "child-session-mcp"
mock_session_manager.register.return_value = child_session

tool_proxy = AsyncMock()
tool_proxy.get_tool_schema.return_value = {
    "success": True,
    "tool": {"inputSchema": {}},
}
tool_proxy.call_tool.return_value = {"success": True, "executions": []}
```

**Why this fails after the refactor.** `execute_mcp_step` at `src/gobby/workflows/pipeline/handlers.py:38-44` now does:

```python
session_manager = tool_proxy.session_manager   # AsyncMock auto-child when tool_proxy is AsyncMock
tokens = resolve_and_seed_contexts(
    session_ref=pipeline_session_id,
    session_manager=session_manager,
    project_ref=None,
    db=(session_manager.db if session_manager else None),
)
effective_session_id = tokens.resolved_session_id
```

Inside `resolve_and_seed_contexts` at `src/gobby/utils/session_context.py:231-233`:

```python
resolved_session_id = str(
    session_manager.resolve_session_reference(session_ref, session_scope)
)
```

`resolve_session_reference` is **synchronous** (confirmed at `src/gobby/sessions/manager.py:477` and `src/gobby/storage/sessions.py:312` — `def`, not `async def`). Calling it on an auto-generated `AsyncMock` returns a coroutine; `str(<coroutine>)` yields the `<coroutine object ...>` repr that breaks the downstream assertion.

**Fix.** Replace lines 2058–2068 with the following block, which (a) attaches `mock_session_manager` to `tool_proxy.session_manager` and (b) stubs `resolve_session_reference` and `get` with the values the helper reads. The additions are marked with comments for reviewer clarity; keep or strip the comments per taste during implementation (prefer strip per CLAUDE.md "no narrating-the-fix comments").

```python
mock_session_manager = MagicMock()
child_session = MagicMock()
child_session.id = "child-session-mcp"
child_session.external_id = "child-session-mcp"
mock_session_manager.register.return_value = child_session
mock_session_manager.resolve_session_reference.return_value = "child-session-mcp"
mock_session_manager.get.return_value = child_session

tool_proxy = AsyncMock()
tool_proxy.session_manager = mock_session_manager
tool_proxy.get_tool_schema.return_value = {
    "success": True,
    "tool": {"inputSchema": {}},
}
tool_proxy.call_tool.return_value = {"success": True, "executions": []}
```

**What each added line is for:**

- `child_session.external_id = "child-session-mcp"` — `session_context.py:244-247` reads `session.external_id` on the result of `session_manager.get(...)` to populate `SessionContext.conversation_id`. Without this, `conversation_id` becomes a raw `MagicMock`; harmless for this test's assertions, but the explicit value keeps the enriched `SessionContext` consistent with `resolved_session_id`.
- `mock_session_manager.resolve_session_reference.return_value = "child-session-mcp"` — **the load-bearing fix.** Ensures the sync resolver returns a real string, so `str(...)` yields `"child-session-mcp"` and `tokens.resolved_session_id` is set correctly.
- `mock_session_manager.get.return_value = child_session` — matches the repo convention (`tests/mcp_proxy/test_mcp_tools_session_messages.py:204-205`) where `resolve_session_reference` and `get` are always stubbed together. Prevents an auto-mocked `external_id` from landing in the seeded `SessionContext`.
- `tool_proxy.session_manager = mock_session_manager` — the whole point: the handler reaches session_manager *via `tool_proxy`*, not via the executor's `session_manager` param. Attaching the explicit stub here overrides `AsyncMock`'s auto-attribute generation.

**What is deliberately NOT wired:**

- No `mock_session_manager.db = ...`. `handlers.py:43` passes `session_manager.db` into `resolve_and_seed_contexts`, but with `project_ref=None` the helper never touches `db` (`_canonicalize_project_ref` short-circuits on `None`). Any auto-mocked attribute suffices — no explicit stub needed.
- No handler-side defensive coding. The handler is correct; the test was wrong.
- No shared fixture refactor. Only one test in this class takes the MCP-step path.

**Pattern alignment.** Every other test in the repo that calls `resolve_session_reference` stubs it explicitly with `return_value = "<uuid>"` — see `tests/mcp_proxy/test_mcp_tools_session_messages.py:204-205, :233, :306, :331, :356, :426, :505` and `tests/hooks/test_hook_manager.py:732`. This change conforms.

**Verification.**

```bash
# Targeted — must pass after the fix
uv run pytest tests/workflows/test_pipeline_executor.py::TestPipelineChildSession::test_mcp_steps_use_child_session_id -v

# Regression check on the full class — must stay green
uv run pytest tests/workflows/test_pipeline_executor.py::TestPipelineChildSession -v

# Confirm the broader file is still clean (excludes unrelated suites)
uv run pytest tests/workflows/test_pipeline_executor.py -v
```

Expected post-fix assertions inside the test:

- `tool_proxy.get_tool_schema.assert_called_once_with("gobby-workflows", "list_pipeline_executions", session_id="child-session-mcp")` passes.
- `tool_proxy.call_tool.assert_called_once_with("gobby-workflows", "list_pipeline_executions", {}, session_id="child-session-mcp")` passes.

## Task Mapping

<!-- Updated after task creation -->
| Plan Item | Task Ref | Status |
|-----------|----------|--------|
| 1.1 Wire mock_session_manager onto tool_proxy | — | Pending expansion |
