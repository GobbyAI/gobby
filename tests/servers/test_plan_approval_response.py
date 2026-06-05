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


def _make_host(*, with_chat_ingress: bool = True) -> SessionControlMixin:
    class MockHost(SessionControlMixin):
        def __init__(self) -> None:
            self._chat_sessions = {}
            self.clients = {}
            self._active_chat_tasks = {}
            self._pending_modes = {}
            self._pending_worktree_paths = {}
            self._pending_agents = {}
            self.session_manager = None

    host = MockHost()
    if with_chat_ingress:
        host._handle_chat_message = AsyncMock()
    return host


def _make_session(*, provider: str, has_pending_plan: bool, plan_auto_switch: bool) -> MagicMock:
    session = MagicMock()
    session.provider = provider
    session.has_pending_plan = has_pending_plan
    session.plan_auto_switch = plan_auto_switch
    session.sync_sdk_permission_mode = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_option_claude_bypass_drives_bypass_mode_no_auto_continue() -> None:
    """Claude approve_bypass option -> bypass mode; native does not auto-continue."""
    host = _make_host()
    session = _make_session(provider="claude", has_pending_plan=True, plan_auto_switch=True)
    host._chat_sessions["c"] = session
    websocket = AsyncMock()

    await SessionControlMixin._handle_plan_approval_response(
        host,
        websocket,
        {"conversation_id": "c", "decision": "approve", "option_id": "approve_bypass"},
    )

    # The registry option drives the post-plan mode, not the configured default.
    session.set_chat_mode.assert_called_once_with("bypass")
    session.provide_plan_decision.assert_called_once_with("approve")
    host._handle_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_option_managed_yolo_drives_bypass_and_auto_continues() -> None:
    """ACP approve_yolo option -> bypass mode + auto-continue on a managed CLI."""
    host = _make_host()
    session = _make_session(provider="gemini", has_pending_plan=True, plan_auto_switch=False)
    host._chat_sessions["c"] = session
    websocket = AsyncMock()

    await SessionControlMixin._handle_plan_approval_response(
        host,
        websocket,
        {"conversation_id": "c", "decision": "approve", "option_id": "approve_yolo"},
    )

    session.set_chat_mode.assert_called_once_with("bypass")
    host._handle_chat_message.assert_awaited_once()
    _, cont = host._handle_chat_message.await_args[0]
    assert cont["type"] == "chat_message"
    assert cont["content"].strip()


@pytest.mark.asyncio
async def test_option_managed_auto_edit_drives_accept_edits_mode() -> None:
    """ACP approve_auto_edit option -> accept_edits mode on a managed CLI."""
    host = _make_host()
    session = _make_session(provider="qwen", has_pending_plan=True, plan_auto_switch=False)
    host._chat_sessions["c"] = session

    await SessionControlMixin._handle_plan_approval_response(
        host,
        AsyncMock(),
        {"conversation_id": "c", "decision": "approve", "option_id": "approve_auto_edit"},
    )

    session.set_chat_mode.assert_called_once_with("accept_edits")
    host._handle_chat_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_option_ultraplan_keeps_plan_pending_native() -> None:
    """Claude ultraplan option keeps the plan unapproved and re-plans deeper."""
    host = _make_host()
    session = _make_session(provider="claude", has_pending_plan=True, plan_auto_switch=True)
    host._chat_sessions["c"] = session

    await SessionControlMixin._handle_plan_approval_response(
        host,
        AsyncMock(),
        {"conversation_id": "c", "decision": "approve", "option_id": "ultraplan"},
    )

    # No mode switch, no approval — the plan stays in planning.
    session.set_chat_mode.assert_not_called()
    session.provide_plan_decision.assert_called_once_with("request_changes")
    feedback_arg = session.set_plan_feedback.call_args[0][0]
    assert "Ultraplan" in feedback_arg
    host._handle_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_option_keep_planning_managed_injects_directive_turn() -> None:
    """ACP keep_planning option on a managed CLI re-plans via an injected turn."""
    host = _make_host()
    session = _make_session(provider="droid", has_pending_plan=False, plan_auto_switch=False)
    host._chat_sessions["c"] = session
    websocket = AsyncMock()

    await SessionControlMixin._handle_plan_approval_response(
        host,
        websocket,
        {"conversation_id": "c", "decision": "approve", "option_id": "keep_planning"},
    )

    session.set_chat_mode.assert_not_called()
    # mode_changed(plan) keeps the UI in planning, then a directive turn is posted.
    sent = json.loads(websocket.send.call_args[0][0])
    assert sent["type"] == "mode_changed"
    assert sent["mode"] == "plan"
    host._handle_chat_message.assert_awaited_once()
    _, cont = host._handle_chat_message.await_args[0]
    assert cont["content"].strip()


@pytest.mark.asyncio
async def test_option_codex_clear_context_resets_and_reseeds_plan() -> None:
    """Codex approve_clear_context clears the thread and re-seeds the plan."""
    host = _make_host()
    session = _make_session(provider="codex", has_pending_plan=True, plan_auto_switch=False)
    session.clear_context = AsyncMock(return_value=True)
    session._last_plan_content = "BUILD THE WIDGET"
    host._chat_sessions["c"] = session

    await SessionControlMixin._handle_plan_approval_response(
        host,
        AsyncMock(),
        {"conversation_id": "c", "decision": "approve", "option_id": "approve_clear_context"},
    )

    session.set_chat_mode.assert_called_once_with("normal")
    session.clear_context.assert_awaited_once()
    host._handle_chat_message.assert_awaited_once()
    _, cont = host._handle_chat_message.await_args[0]
    # The cleared context is re-seeded with the approved plan body.
    assert "BUILD THE WIDGET" in cont["content"]


@pytest.mark.asyncio
async def test_unknown_option_id_falls_back_to_generic_approve() -> None:
    """An unresolved option_id preserves legacy generic-approve behavior."""
    host = _make_host()
    session = _make_session(provider="claude", has_pending_plan=True, plan_auto_switch=True)
    host._chat_sessions["c"] = session

    with patch(
        "gobby.servers.websocket.handlers.plan_approval._resolve_post_plan_mode",
        return_value="normal",
    ):
        await SessionControlMixin._handle_plan_approval_response(
            host,
            AsyncMock(),
            {"conversation_id": "c", "decision": "approve", "option_id": "no_such_option"},
        )

    # Falls back to the configured post-plan default, not an option mode.
    session.set_chat_mode.assert_called_once_with("normal")
    session.provide_plan_decision.assert_called_once_with("approve")
