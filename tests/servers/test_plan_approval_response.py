"""Tests for plan_approval_response WebSocket handler.

Verified for task #10454: Backend must emit mode_changed for request_changes (non-ExitPlanMode path).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.websocket.session_control import SessionControlMixin


@pytest.mark.asyncio
async def test_handle_plan_approval_request_changes_legacy_sends_mode_changed():
    """Test that request_changes in the legacy path (no pending plan) sends mode_changed."""

    # Create a host that implements the mixin's required attributes
    class MockHost(SessionControlMixin):
        def __init__(self):
            self._chat_sessions = {}
            self.clients = {}
            self._active_chat_tasks = {}
            self._pending_modes = {}
            self._pending_worktree_paths = {}
            self._pending_agents = {}
            self.session_manager = None

    host = MockHost()

    # Mock a session
    session = MagicMock()
    session.has_pending_plan = False
    session.chat_mode = "plan"

    conversation_id = "test-conv-id"
    host._chat_sessions[conversation_id] = session

    # Mock a websocket
    websocket = AsyncMock()

    # Data for request_changes
    data = {
        "type": "plan_approval_response",
        "conversation_id": conversation_id,
        "decision": "request_changes",
        "feedback": "Please fix the typo.",
    }

    # Call the handler
    # We call it directly from the class since it's a mixin and we want to test its implementation
    await SessionControlMixin._handle_plan_approval_response(host, websocket, data)

    # Verify feedback was set
    session.set_plan_feedback.assert_called_once_with("Please fix the typo.")

    # Verify mode_changed was sent via websocket
    # THIS SHOULD FAIL BEFORE THE FIX
    websocket.send.assert_called_once()
    sent_data = json.loads(websocket.send.call_args[0][0])
    assert sent_data["type"] == "mode_changed"
    assert sent_data["conversation_id"] == conversation_id
    assert sent_data["mode"] == "plan"
    assert sent_data["reason"] == "plan_changes_requested"


@pytest.mark.asyncio
async def test_handle_plan_approval_approve_legacy_sends_mode_changed():
    """Verify approve exits plan mode into the configured post-plan mode."""

    class MockHost(SessionControlMixin):
        def __init__(self):
            self._chat_sessions = {}
            self.clients = {}
            self._active_chat_tasks = {}
            self._pending_modes = {}
            self._pending_worktree_paths = {}
            self._pending_agents = {}

    host = MockHost()

    session = MagicMock()
    session.has_pending_plan = False
    session.sync_sdk_permission_mode = AsyncMock()

    conversation_id = "test-conv-id"
    host._chat_sessions[conversation_id] = session

    websocket = AsyncMock()

    data = {
        "type": "plan_approval_response",
        "conversation_id": conversation_id,
        "decision": "approve",
    }

    with patch(
        "gobby.servers.websocket.handlers.plan_approval._resolve_post_plan_mode",
        return_value="bypass",
    ):
        await SessionControlMixin._handle_plan_approval_response(host, websocket, data)

    session.approve_plan.assert_called_once()
    session.set_chat_mode.assert_called_once_with("bypass")
    session.sync_sdk_permission_mode.assert_awaited_once()

    websocket.send.assert_called_once()
    sent_data = json.loads(websocket.send.call_args[0][0])
    assert sent_data["type"] == "mode_changed"
    assert sent_data["mode"] == "bypass"
    assert sent_data["reason"] == "plan_approved"


@pytest.mark.asyncio
async def test_handle_plan_approval_approve_pending_plan_unblocks_into_post_plan_mode():
    """Approve while ExitPlanMode is pending should switch mode before unblocking."""

    class MockHost(SessionControlMixin):
        def __init__(self):
            self._chat_sessions = {}
            self.clients = {}
            self._active_chat_tasks = {}
            self._pending_modes = {}
            self._pending_worktree_paths = {}
            self._pending_agents = {}
            self.session_manager = None

    host = MockHost()

    session = MagicMock()
    session.has_pending_plan = True
    session.sync_sdk_permission_mode = AsyncMock()

    conversation_id = "test-conv-id"
    host._chat_sessions[conversation_id] = session

    websocket = AsyncMock()

    data = {
        "type": "plan_approval_response",
        "conversation_id": conversation_id,
        "decision": "approve",
    }

    with patch(
        "gobby.servers.websocket.handlers.plan_approval._resolve_post_plan_mode",
        return_value="normal",
    ):
        await SessionControlMixin._handle_plan_approval_response(host, websocket, data)

    session.set_chat_mode.assert_called_once_with("normal")
    session.provide_plan_decision.assert_called_once_with("approve")
    session.sync_sdk_permission_mode.assert_awaited_once()
    websocket.send.assert_not_called()
