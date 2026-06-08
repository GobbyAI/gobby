"""Tests for plan_approval_response WebSocket handler.

The accept options are uniform: ``approve_yolo`` (-> bypass) and ``approve_act``
(-> normal). Reject is ``request_changes`` with an optional comment. A missing
or unknown ``option_id`` falls back to the generic-approve default (normal mode,
auto-continue) so older clients stay compatible.
"""

import json
from unittest.mock import AsyncMock, MagicMock

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
    session.has_blocking_plan_decision = False
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
    """Approve with no option_id exits plan mode into the generic default (normal)."""

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
    session.provider = "claude"
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

    await SessionControlMixin._handle_plan_approval_response(host, websocket, data)

    session.approve_plan.assert_called_once()
    session.set_chat_mode.assert_called_once_with("normal")
    session.sync_sdk_permission_mode.assert_awaited_once()

    websocket.send.assert_called_once()
    sent_data = json.loads(websocket.send.call_args[0][0])
    assert sent_data["type"] == "mode_changed"
    assert sent_data["mode"] == "normal"
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
    session.provider = "claude"
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
    session.provider = "droid"
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
    session.provider = "claude"
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
    # Default: no blocking plan-decision gate (text-plan CLIs). Tests that model
    # a tool-plan CLI (Droid ExitSpecMode) override this to True. Without an
    # explicit value a MagicMock attribute is truthy, which would wrongly trip
    # the request_changes blocking-gate branch.
    session.has_blocking_plan_decision = False
    session.sync_sdk_permission_mode = AsyncMock()
    return session


@pytest.mark.asyncio
async def test_option_yolo_native_drives_bypass_no_auto_continue() -> None:
    """approve_yolo -> bypass mode; native Claude does not auto-continue."""
    host = _make_host()
    session = _make_session(provider="claude", has_pending_plan=True, plan_auto_switch=True)
    host._chat_sessions["c"] = session
    websocket = AsyncMock()

    await SessionControlMixin._handle_plan_approval_response(
        host,
        websocket,
        {"conversation_id": "c", "decision": "approve", "option_id": "approve_yolo"},
    )

    # The registry option drives the post-plan mode.
    session.set_chat_mode.assert_called_once_with("bypass")
    session.provide_plan_decision.assert_called_once_with("approve")
    host._handle_chat_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_option_yolo_managed_drives_bypass_and_auto_continues() -> None:
    """approve_yolo -> bypass mode + auto-continue on a managed CLI."""
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
async def test_option_act_managed_drives_normal_mode_and_auto_continues() -> None:
    """approve_act -> normal mode + auto-continue on a managed CLI."""
    host = _make_host()
    session = _make_session(provider="qwen", has_pending_plan=True, plan_auto_switch=False)
    host._chat_sessions["c"] = session

    await SessionControlMixin._handle_plan_approval_response(
        host,
        AsyncMock(),
        {"conversation_id": "c", "decision": "approve", "option_id": "approve_act"},
    )

    session.set_chat_mode.assert_called_once_with("normal")
    host._handle_chat_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_option_act_droid_blocking_gate_still_auto_continues() -> None:
    """Regression guard (#15619): a tool-plan CLI (Droid ExitSpecMode) parks
    approval on the plan-decision gate, then ENDS its turn once released -- it
    does not auto-execute natively (verified live). The approve path must still
    inject a continuation for a blocking-gate CLI, so do NOT re-add a
    `not blocking_plan` skip here or the approved plan just sits idle."""
    host = _make_host()
    session = _make_session(provider="droid", has_pending_plan=True, plan_auto_switch=False)
    session.has_blocking_plan_decision = True
    host._chat_sessions["c"] = session

    await SessionControlMixin._handle_plan_approval_response(
        host,
        AsyncMock(),
        {"conversation_id": "c", "decision": "approve", "option_id": "approve_act"},
    )

    session.set_chat_mode.assert_called_once_with("normal")
    session.provide_plan_decision.assert_called_once_with("approve")
    host._handle_chat_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_reject_with_empty_comment_denies_pending_plan() -> None:
    """Reject (request_changes) with no comment denies ExitPlanMode; no feedback set."""
    host = _make_host()
    session = _make_session(provider="claude", has_pending_plan=True, plan_auto_switch=True)
    host._chat_sessions["c"] = session

    await SessionControlMixin._handle_plan_approval_response(
        host,
        AsyncMock(),
        {"conversation_id": "c", "decision": "request_changes"},
    )

    # The comment is optional, so no feedback is recorded when empty.
    session.set_plan_feedback.assert_not_called()
    session.provide_plan_decision.assert_called_once_with("request_changes")
    session.set_chat_mode.assert_not_called()


@pytest.mark.asyncio
async def test_reject_with_empty_comment_managed_returns_to_plan() -> None:
    """Reject with no comment on a managed CLI returns the UI to plan mode."""
    host = _make_host()
    session = _make_session(provider="droid", has_pending_plan=False, plan_auto_switch=False)
    host._chat_sessions["c"] = session
    websocket = AsyncMock()

    await SessionControlMixin._handle_plan_approval_response(
        host,
        websocket,
        {"conversation_id": "c", "decision": "request_changes"},
    )

    session.set_plan_feedback.assert_not_called()
    sent = json.loads(websocket.send.call_args[0][0])
    assert sent["type"] == "mode_changed"
    assert sent["mode"] == "plan"


@pytest.mark.asyncio
async def test_unknown_option_id_falls_back_to_generic_approve() -> None:
    """An unresolved option_id preserves the legacy generic-approve default."""
    host = _make_host()
    session = _make_session(provider="claude", has_pending_plan=True, plan_auto_switch=True)
    host._chat_sessions["c"] = session

    await SessionControlMixin._handle_plan_approval_response(
        host,
        AsyncMock(),
        {"conversation_id": "c", "decision": "approve", "option_id": "no_such_option"},
    )

    # Falls back to the generic post-plan default (normal), not an option mode.
    session.set_chat_mode.assert_called_once_with("normal")
    session.provide_plan_decision.assert_called_once_with("approve")
