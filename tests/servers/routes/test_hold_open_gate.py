"""Tests for _maybe_hold_open in hooks route.

Covers:
- Terminal sessions fall through (return None)
- No database returns None
- Unknown session returns None
- Web chat PreToolUse creates pending interaction and returns decision
- Web chat AskUserQuestion creates pending interaction and returns response
- Rate-limit: too many pending interactions returns deny
- Timeout/expired interaction returns deny
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import State

from gobby.app_context import ServiceContainer
from gobby.config.bootstrap import BootstrapConfig
from gobby.config.runtime import ConfigRuntime
from gobby.servers.http import HTTPServer
from gobby.servers.routes.mcp.hook_hold_open import MAX_PENDING_PER_SESSION, _maybe_hold_open
from gobby.storage.sessions import SessionManager
from tests.servers.conftest import authenticate_test_server

pytestmark = pytest.mark.unit

_SESSION_MANAGER_PATCH = "gobby.storage.sessions.SessionManager"


def _hook_envelope(**payload: Any) -> dict[str, Any]:
    from gobby.hooks.runtime_compat import SUPPORTED_HOOK_RESPONSE_CAPABILITY

    envelope = {
        "schema_version": 1,
        "enqueued_at": "2026-04-16T12:00:00Z",
        "critical": False,
        "response_capability": SUPPORTED_HOOK_RESPONSE_CAPABILITY,
        "input_data": {},
    }
    envelope.update(payload)
    return envelope


def _make_request(
    db: object | None = None,
    pending_manager: object | None = None,
) -> MagicMock:
    """Build a fake Request with app.state wired up."""
    request = MagicMock()
    request.app.state.server.services.database = db

    async def _run_db_passthrough(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    request.app.state.server.run_db = AsyncMock(side_effect=_run_db_passthrough)
    if pending_manager is not None:
        request.app.state.pending_interaction_manager = pending_manager
    else:
        # Simulate attribute missing
        del request.app.state.pending_interaction_manager
    return request


def _make_session(session_id: str = "sess-1", session_type: str = "terminal") -> SimpleNamespace:
    return SimpleNamespace(id=session_id, session_type=session_type)


@pytest.mark.parametrize(
    "denial",
    [
        {
            "hookSpecificOutput": {
                "permissionDecision": "deny",
                "permissionDecisionReason": "blocked by rule",
            }
        },
        {"permissionDecision": "deny", "reason": "blocked by rule"},
        {"continue": True, "decision": "block", "reason": "blocked by rule"},
        {"continue": False, "decision": "approve", "reason": "blocked by rule"},
    ],
    ids=["nested-permission-deny", "permission-deny", "decision-block", "continue-false"],
)
def test_rule_denial_short_circuits_before_web_chat_auto_approval(
    session_storage: SessionManager,
    denial: dict[str, Any],
) -> None:
    """A rule deny wins even when hold-open would auto-approve the tool."""
    services = ServiceContainer(
        database=session_storage.db,
        session_manager=session_storage,
        task_manager=MagicMock(),
    )
    server = HTTPServer(
        services=services,
        port=60887,
        test_mode=True,
        bootstrap_config=BootstrapConfig(),
    )
    authenticate_test_server(server)
    mock_hook_manager = MagicMock()
    server.app.state.hook_manager = mock_hook_manager

    with (
        patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as MockAdapter,
        patch(
            "gobby.servers.routes.mcp.hook_hold_open._maybe_hold_open",
            new_callable=AsyncMock,
            return_value={"decision": "approve"},
        ) as hold_open,
    ):
        mock_adapter_instance = MagicMock()
        mock_adapter_instance.handle_native.return_value = denial
        MockAdapter.return_value = mock_adapter_instance
        response = TestClient(server.app).post(
            "/api/hooks/execute",
            headers={"X-Gobby-Session-Id": "web-chat-session"},
            json=_hook_envelope(
                hook_type="PreToolUse",
                source="claude",
                input_data={"tool_name": "Read", "arguments": {"path": "/tmp/file"}},
            ),
        )

    assert response.status_code == 200
    assert response.json() == denial
    hold_open.assert_not_awaited()


def test_allowed_result_still_uses_web_chat_hold_open(
    session_storage: SessionManager,
) -> None:
    services = ServiceContainer(
        database=session_storage.db,
        session_manager=session_storage,
        task_manager=MagicMock(),
    )
    server = HTTPServer(
        services=services,
        port=60887,
        test_mode=True,
        bootstrap_config=BootstrapConfig(),
    )
    authenticate_test_server(server)
    mock_hook_manager = MagicMock()
    server.app.state.hook_manager = mock_hook_manager
    hold_open_result = {"decision": "approve", "reason": "web-chat approval"}

    with (
        patch("gobby.adapters.claude_code.ClaudeCodeAdapter") as MockAdapter,
        patch(
            "gobby.servers.routes.mcp.hook_hold_open._maybe_hold_open",
            new_callable=AsyncMock,
            return_value=hold_open_result,
        ) as hold_open,
    ):
        mock_adapter_instance = MagicMock()
        mock_adapter_instance.handle_native.return_value = {"continue": True}
        MockAdapter.return_value = mock_adapter_instance
        response = TestClient(server.app).post(
            "/api/hooks/execute",
            headers={"X-Gobby-Session-Id": "web-chat-session"},
            json=_hook_envelope(
                hook_type="PreToolUse",
                source="claude",
                input_data={"tool_name": "Write", "arguments": {"path": "/tmp/file"}},
            ),
        )

    assert response.status_code == 200
    assert response.json() == hold_open_result
    hold_open.assert_awaited_once()


# --- Early exits ---


@pytest.mark.asyncio
async def test_missing_server_state_returns_none() -> None:
    request = MagicMock()
    request.app.state = State()

    result = await _maybe_hold_open(request, "sess-1", "PreToolUse", {}, "claude")

    assert result is None


@pytest.mark.asyncio
async def test_no_database_returns_none() -> None:
    request = _make_request(db=None)
    result = await _maybe_hold_open(request, "sess-1", "PreToolUse", {}, "claude")
    assert result is None


@pytest.mark.asyncio
async def test_unknown_session_returns_none() -> None:
    db = MagicMock()
    request = _make_request(db=db)

    with patch(_SESSION_MANAGER_PATCH) as MockSM:
        MockSM.return_value.get.return_value = None
        result = await _maybe_hold_open(request, "no-such", "PreToolUse", {}, "claude")

    assert result is None


@pytest.mark.asyncio
async def test_terminal_session_returns_none() -> None:
    db = MagicMock()
    session = _make_session(session_type="terminal")
    request = _make_request(db=db)

    with patch(_SESSION_MANAGER_PATCH) as MockSM:
        MockSM.return_value.get.return_value = session
        result = await _maybe_hold_open(request, "sess-1", "PreToolUse", {}, "claude")

    assert result is None


@pytest.mark.asyncio
async def test_no_pending_manager_returns_none() -> None:
    db = MagicMock()
    session = _make_session(session_type="web_chat")
    request = _make_request(db=db, pending_manager=None)

    with patch(_SESSION_MANAGER_PATCH) as MockSM:
        MockSM.return_value.get.return_value = session
        result = await _maybe_hold_open(request, "sess-1", "PreToolUse", {}, "claude")

    assert result is None


# --- PreToolUse ---


@pytest.mark.asyncio
async def test_web_chat_pre_tool_use_approve() -> None:
    db = MagicMock()
    session = _make_session(session_id="sess-web-1", session_type="web_chat")
    manager = AsyncMock()
    manager.count_pending.return_value = 0
    manager.create.return_value = "interaction-1"
    manager.wait.return_value = {"decision": "approve"}

    request = _make_request(db=db, pending_manager=manager)
    payload = {"input_data": {"tool_name": "bash", "arguments": {"command": "ls"}}}

    with patch(_SESSION_MANAGER_PATCH) as MockSM:
        MockSM.return_value.get.return_value = session
        result = await _maybe_hold_open(request, "sess-web-1", "PreToolUse", payload, "web_chat")

    assert result == {"decision": "approve"}
    manager.create.assert_called_once()
    manager.wait.assert_called_once_with("interaction-1")


@pytest.mark.asyncio
async def test_web_chat_pre_tool_use_startup_returns_retryable_503() -> None:
    session = _make_session(session_id="sess-web-1", session_type="web_chat")
    manager = AsyncMock()
    request = _make_request(db=MagicMock(), pending_manager=manager)
    runtime = MagicMock(spec=ConfigRuntime)
    type(runtime).snapshot = PropertyMock(side_effect=RuntimeError("runtime starting"))
    request.app.state.server.services.config_runtime = runtime

    with (
        patch(_SESSION_MANAGER_PATCH) as mock_session_manager,
        pytest.raises(HTTPException) as raised,
    ):
        mock_session_manager.return_value.get.return_value = session
        await _maybe_hold_open(
            request,
            "sess-web-1",
            "PreToolUse",
            {"input_data": {"tool_name": "bash", "arguments": {}}},
            "web_chat",
        )

    detail = cast(dict[str, object], raised.value.detail)
    assert raised.value.status_code == 503
    assert detail["code"] == "runtime_unavailable"
    assert detail["retryable"] is True


@pytest.mark.asyncio
async def test_web_chat_pre_tool_use_falls_back_to_external_id_lookup() -> None:
    db = MagicMock()
    session = _make_session(session_id="sess-web-1", session_type="web_chat")
    manager = AsyncMock()
    manager.count_pending.return_value = 0
    manager.create.return_value = "interaction-1"
    manager.wait.return_value = {"decision": "approve"}

    request = _make_request(db=db, pending_manager=manager)
    payload = {"input_data": {"tool_name": "bash", "arguments": {"command": "ls"}}}

    with patch(_SESSION_MANAGER_PATCH) as MockSM:
        store = MockSM.return_value
        store.get.return_value = None
        store.resolve_session_reference.side_effect = ValueError("not a session ref")
        store.find_active_by_external_id.return_value = session
        result = await _maybe_hold_open(
            request, "provider-session-123", "PreToolUse", payload, "claude"
        )

    assert result == {"decision": "approve"}
    store.find_active_by_external_id.assert_called_once_with("provider-session-123", "claude")
    manager.create.assert_called_once()
    manager.wait.assert_called_once_with("interaction-1")


@pytest.mark.asyncio
async def test_web_chat_pre_tool_use_deny_on_timeout() -> None:
    db = MagicMock()
    session = _make_session(session_id="sess-web-1", session_type="web_chat")
    manager = AsyncMock()
    manager.count_pending.return_value = 0
    manager.create.return_value = "interaction-1"
    manager.wait.return_value = {"decision": "expired"}

    request = _make_request(db=db, pending_manager=manager)
    payload = {"input_data": {"tool_name": "bash", "arguments": {}}}

    with patch(_SESSION_MANAGER_PATCH) as MockSM:
        MockSM.return_value.get.return_value = session
        result = await _maybe_hold_open(request, "sess-web-1", "PreToolUse", payload, "web_chat")

    assert result == {"decision": "deny"}


@pytest.mark.asyncio
async def test_web_chat_rate_limit_deny() -> None:
    db = MagicMock()
    session = _make_session(session_id="sess-web-1", session_type="web_chat")
    manager = AsyncMock()
    manager.count_pending.return_value = MAX_PENDING_PER_SESSION

    request = _make_request(db=db, pending_manager=manager)
    payload = {"input_data": {"tool_name": "bash", "arguments": {}}}

    with patch(_SESSION_MANAGER_PATCH) as MockSM:
        MockSM.return_value.get.return_value = session
        result = await _maybe_hold_open(request, "sess-web-1", "PreToolUse", payload, "web_chat")

    assert result == {"decision": "deny", "reason": "too_many_pending"}
    manager.create.assert_not_called()


# --- AskUserQuestion ---


@pytest.mark.asyncio
async def test_web_chat_ask_user_question() -> None:
    db = MagicMock()
    session = _make_session(session_id="sess-web-1", session_type="web_chat")
    manager = AsyncMock()
    manager.create.return_value = "interaction-2"
    manager.wait.return_value = {"response": {"answers": {"q1": "yes"}}}

    request = _make_request(db=db, pending_manager=manager)
    payload = {"input_data": {"question": "Continue?"}}

    with patch(_SESSION_MANAGER_PATCH) as MockSM:
        MockSM.return_value.get.return_value = session
        result = await _maybe_hold_open(
            request, "sess-web-1", "AskUserQuestion", payload, "web_chat"
        )

    assert result == {"additionalContext": {"q1": "yes"}}


# --- Unsupported hook type ---


@pytest.mark.asyncio
async def test_unsupported_hook_type_returns_none() -> None:
    db = MagicMock()
    session = _make_session(session_type="web_chat")
    manager = AsyncMock()

    request = _make_request(db=db, pending_manager=manager)

    with patch(_SESSION_MANAGER_PATCH) as MockSM:
        MockSM.return_value.get.return_value = session
        result = await _maybe_hold_open(request, "sess-1", "Stop", {}, "web_chat")

    assert result is None
