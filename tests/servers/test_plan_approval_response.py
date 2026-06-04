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
    assert session.set_chat_mode.call_count == 1
    assert session.set_chat_mode.call_args is not None
    session.provide_plan_decision.assert_called_once_with("approve")
    assert session.provide_plan_decision.call_count == 1
    assert session.provide_plan_decision.call_args is not None
    session.sync_sdk_permission_mode.assert_awaited_once()
    assert session.sync_sdk_permission_mode.await_count == 1
    assert session.sync_sdk_permission_mode.await_args is not None
    websocket.send.assert_not_called()
    assert websocket.send.call_count == 0
    assert not websocket.send.called


@pytest.mark.asyncio
async def test_handle_plan_approval_approve_managed_auto_continues_execution():
    """Approve on a managed CLI auto-starts a continuation turn (#15633).

    Managed CLIs (plan_auto_switch False) present a plan as a completed
    assistant turn with no ExitPlanMode to unblock, so approval must inject a
    continuation through the normal chat-message ingress for execution to begin.
    """

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
    host._handle_chat_message = AsyncMock()

    session = MagicMock()
    session.has_pending_plan = True
    session.plan_auto_switch = False
    session.sync_sdk_permission_mode = AsyncMock()

    conversation_id = "conv-managed"
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
    # Exactly one continuation turn through the chat ingress, on the same socket.
    host._handle_chat_message.assert_awaited_once()
    cont_ws, cont_data = host._handle_chat_message.await_args[0]
    assert cont_ws is websocket
    assert cont_data["type"] == "chat_message"
    assert cont_data["conversation_id"] == conversation_id
    assert cont_data["content"].strip()


@pytest.mark.asyncio
async def test_handle_plan_approval_approve_native_does_not_auto_continue():
    """Native Claude (auto-switch) unblocks ExitPlanMode in-flight; no extra turn."""

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
    host._handle_chat_message = AsyncMock()

    session = MagicMock()
    session.has_pending_plan = True
    session.plan_auto_switch = True
    session.sync_sdk_permission_mode = AsyncMock()

    conversation_id = "conv-native"
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

    # Native unblocks ExitPlanMode in-flight and switches mode, but does NOT
    # inject a second turn (that would double-execute the approved plan).
    session.set_chat_mode.assert_called_once_with("normal")
    session.sync_sdk_permission_mode.assert_awaited_once()
    session.provide_plan_decision.assert_called_once_with("approve")
    host._handle_chat_message.assert_not_awaited()
