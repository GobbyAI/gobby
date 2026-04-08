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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.routes.mcp.hooks import _maybe_hold_open

pytestmark = pytest.mark.unit

_LSM_PATCH = "gobby.storage.sessions.LocalSessionManager"


def _make_request(
    db: object | None = None,
    pending_manager: object | None = None,
) -> MagicMock:
    """Build a fake Request with app.state wired up."""
    request = MagicMock()
    request.app.state.server.services.database = db
    if pending_manager is not None:
        request.app.state.pending_interaction_manager = pending_manager
    else:
        # Simulate attribute missing
        del request.app.state.pending_interaction_manager
    return request


def _make_session(session_id: str = "sess-1", session_type: str = "terminal") -> SimpleNamespace:
    return SimpleNamespace(id=session_id, session_type=session_type)


# --- Early exits ---


@pytest.mark.asyncio
async def test_no_database_returns_none() -> None:
    request = _make_request(db=None)
    result = await _maybe_hold_open(request, "sess-1", "PreToolUse", {}, "claude")
    assert result is None


@pytest.mark.asyncio
async def test_unknown_session_returns_none() -> None:
    db = MagicMock()
    request = _make_request(db=db)

    with patch(_LSM_PATCH) as MockSM:
        MockSM.return_value.get.return_value = None
        result = await _maybe_hold_open(request, "no-such", "PreToolUse", {}, "claude")

    assert result is None


@pytest.mark.asyncio
async def test_terminal_session_returns_none() -> None:
    db = MagicMock()
    session = _make_session(session_type="terminal")
    request = _make_request(db=db)

    with patch(_LSM_PATCH) as MockSM:
        MockSM.return_value.get.return_value = session
        result = await _maybe_hold_open(request, "sess-1", "PreToolUse", {}, "claude")

    assert result is None


@pytest.mark.asyncio
async def test_no_pending_manager_returns_none() -> None:
    db = MagicMock()
    session = _make_session(session_type="web_chat")
    request = _make_request(db=db, pending_manager=None)

    with patch(_LSM_PATCH) as MockSM:
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

    with patch(_LSM_PATCH) as MockSM:
        MockSM.return_value.get.return_value = session
        result = await _maybe_hold_open(request, "sess-web-1", "PreToolUse", payload, "web_chat")

    assert result == {"decision": "approve"}
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

    with patch(_LSM_PATCH) as MockSM:
        MockSM.return_value.get.return_value = session
        result = await _maybe_hold_open(request, "sess-web-1", "PreToolUse", payload, "web_chat")

    assert result == {"decision": "expired"}


@pytest.mark.asyncio
async def test_web_chat_rate_limit_deny() -> None:
    db = MagicMock()
    session = _make_session(session_id="sess-web-1", session_type="web_chat")
    manager = AsyncMock()
    manager.count_pending.return_value = 3  # MAX_PENDING_PER_SESSION

    request = _make_request(db=db, pending_manager=manager)
    payload = {"input_data": {"tool_name": "bash", "arguments": {}}}

    with patch(_LSM_PATCH) as MockSM:
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

    with patch(_LSM_PATCH) as MockSM:
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

    with patch(_LSM_PATCH) as MockSM:
        MockSM.return_value.get.return_value = session
        result = await _maybe_hold_open(request, "sess-1", "Stop", {}, "web_chat")

    assert result is None
