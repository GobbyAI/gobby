"""Tests for the attached (proxy-terminal) plan-approval keystroke path.

Path B: a CLI running in a tmux pane has no in-memory ChatSession, so plan
approval drives the native TUI menu via keystrokes sent to the pane. These
tests use a synthetic ``example`` source with a registered sequence -- not any
real CLI's keystrokes.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.adapters.plan_keystrokes import (
    REQUEST_CHANGES_OPTION_ID,
    PlanKeystroke,
    PlanKeystrokeRegistry,
    PlanKeystrokeSequence,
)
from gobby.servers.websocket.handlers.plan_approval import (
    handle_attached_plan_approval,
    handle_plan_approval_response,
)
from gobby.servers.websocket.session_control import SessionControlMixin

_TMUX_PATCH = "gobby.servers.websocket.handlers.plan_approval.get_tmux_manager_for_context"


class ConcreteSessionControl(SessionControlMixin):
    """Minimal concrete SessionControlMixin for handler tests."""

    def __init__(self) -> None:
        self.clients: dict[Any, dict[str, Any]] = {}
        self._chat_sessions: dict[str, Any] = {}
        self._active_chat_tasks: dict[str, Any] = {}
        self._pending_modes: dict[str, str] = {}
        self._pending_worktree_paths: dict[str, str] = {}
        self._pending_agents: dict[str, str] = {}
        self._pending_projects: dict[str, str] = {}
        self._pending_providers: dict[str, str] = {}
        self._send_error = AsyncMock()
        self.session_manager = MagicMock()


def _make_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send = AsyncMock()
    return ws


def _make_terminal_session(
    *,
    source: str = "example",
    tmux_pane: str | None = "%11",
    session_type: str = "terminal",
) -> MagicMock:
    session = MagicMock()
    session.session_type = session_type
    session.source = source
    session.terminal_context = {"tmux_pane": tmux_pane} if tmux_pane else {}
    session.metadata = None
    return session


def _registry_with(
    source: str, option_id: str, seq: PlanKeystrokeSequence
) -> PlanKeystrokeRegistry:
    registry = PlanKeystrokeRegistry()
    registry.register(source, option_id, seq)
    return registry


class TestAttachedPlanApprovalDispatch:
    @pytest.mark.asyncio
    async def test_approve_dispatches_registered_keystrokes(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        server.session_manager.get.return_value = _make_terminal_session()

        registry = _registry_with(
            "example",
            "approve_yolo",
            PlanKeystrokeSequence(
                strokes=(PlanKeystroke("1", literal=True), PlanKeystroke("Enter")),
                settle_seconds=0.0,
            ),
        )
        tmux_manager = MagicMock()
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with patch(_TMUX_PATCH, return_value=tmux_manager) as get_tmux:
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_yolo"},
                registry=registry,
            )

        get_tmux.assert_called_once_with({"tmux_pane": "%11"})
        assert tmux_manager.send_keys.await_args_list[0].args == ("%11", "1")
        assert tmux_manager.send_keys.await_args_list[0].kwargs == {"literal": True}
        assert tmux_manager.send_keys.await_args_list[1].args == ("%11", "Enter")
        assert tmux_manager.send_keys.await_args_list[1].kwargs == {"literal": False}
        msg = json.loads(ws.send.await_args.args[0])
        assert msg == {
            "type": "plan_approval_dispatched",
            "target_session_id": "term-1",
            "decision": "approve",
            "option_id": "approve_yolo",
            "ok": True,
        }
        server._send_error.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_request_changes_dispatches_keep_planning(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        server.session_manager.get.return_value = _make_terminal_session()

        registry = _registry_with(
            "example",
            REQUEST_CHANGES_OPTION_ID,
            PlanKeystrokeSequence(strokes=(PlanKeystroke("3", literal=True),), settle_seconds=0.0),
        )
        tmux_manager = MagicMock()
        tmux_manager.send_keys = AsyncMock(return_value=True)

        with patch(_TMUX_PATCH, return_value=tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "request_changes", "feedback": "tighten it"},
                registry=registry,
            )

        tmux_manager.send_keys.assert_awaited_once_with("%11", "3", literal=True)
        msg = json.loads(ws.send.await_args.args[0])
        assert msg["decision"] == "request_changes"
        assert msg["option_id"] == REQUEST_CHANGES_OPTION_ID
        server._send_error.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unmapped_source_errors(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        server.session_manager.get.return_value = _make_terminal_session(source="claude")

        await handle_attached_plan_approval(
            server,
            ws,
            "term-1",
            {"decision": "approve", "option_id": "approve_yolo"},
            registry=PlanKeystrokeRegistry(),
        )

        server._send_error.assert_awaited_once()
        assert server._send_error.await_args.kwargs.get("code") == "PLAN_KEYSTROKES_UNMAPPED"
        ws.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_terminal_session_errors(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        server.session_manager.get.return_value = _make_terminal_session(session_type="web_chat")

        await handle_attached_plan_approval(
            server,
            ws,
            "term-1",
            {"decision": "approve", "option_id": "approve_yolo"},
            registry=PlanKeystrokeRegistry(),
        )

        server._send_error.assert_awaited_once()
        assert server._send_error.await_args.kwargs.get("code") == "UNSUPPORTED_SESSION_TYPE"

    @pytest.mark.asyncio
    async def test_missing_pane_errors(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        server.session_manager.get.return_value = _make_terminal_session(tmux_pane=None)

        await handle_attached_plan_approval(
            server,
            ws,
            "term-1",
            {"decision": "approve", "option_id": "approve_yolo"},
            registry=PlanKeystrokeRegistry(),
        )

        server._send_error.assert_awaited_once()
        assert server._send_error.await_args.kwargs.get("code") == "NO_TERMINAL_TARGET"

    @pytest.mark.asyncio
    async def test_invalid_decision_errors(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        server.session_manager.get.return_value = _make_terminal_session()

        # approve without an option_id cannot resolve which menu item to select.
        await handle_attached_plan_approval(
            server,
            ws,
            "term-1",
            {"decision": "approve"},
            registry=PlanKeystrokeRegistry(),
        )

        server._send_error.assert_awaited_once()
        assert server._send_error.await_args.kwargs.get("code") == "INVALID_PLAN_DECISION"

    @pytest.mark.asyncio
    async def test_session_not_found_errors(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        server.session_manager.get.return_value = None

        await handle_attached_plan_approval(
            server,
            ws,
            "term-1",
            {"decision": "approve", "option_id": "approve_yolo"},
            registry=PlanKeystrokeRegistry(),
        )

        server._send_error.assert_awaited_once()
        assert server._send_error.await_args.kwargs.get("code") == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_failed_send_reports_error(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()
        server.session_manager.get.return_value = _make_terminal_session()

        registry = _registry_with(
            "example",
            "approve_act",
            PlanKeystrokeSequence(strokes=(PlanKeystroke("2", literal=True),), settle_seconds=0.0),
        )
        tmux_manager = MagicMock()
        tmux_manager.send_keys = AsyncMock(return_value=False)

        with patch(_TMUX_PATCH, return_value=tmux_manager):
            await handle_attached_plan_approval(
                server,
                ws,
                "term-1",
                {"decision": "approve", "option_id": "approve_act"},
                registry=registry,
            )

        server._send_error.assert_awaited_once()
        # The surfaced error names the keystroke-send failure, and no
        # confirmation frame is emitted when the dispatch did not complete.
        assert "keystrokes" in server._send_error.await_args.args[1]
        ws.send.assert_not_awaited()


class TestPlanApprovalRouting:
    @pytest.mark.asyncio
    async def test_target_session_id_routes_to_attached_handler(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        with patch(
            "gobby.servers.websocket.handlers.plan_approval.handle_attached_plan_approval",
            new=AsyncMock(),
        ) as attached:
            await handle_plan_approval_response(
                server,
                ws,
                {
                    "target_session_id": "term-1",
                    "decision": "approve",
                    "option_id": "approve_yolo",
                },
            )

        attached.assert_awaited_once()
        assert attached.await_args.args[2] == "term-1"

    @pytest.mark.asyncio
    async def test_no_target_session_id_uses_conversation_path(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        with patch(
            "gobby.servers.websocket.handlers.plan_approval.handle_attached_plan_approval",
            new=AsyncMock(),
        ) as attached:
            # Unknown conversation falls through to recovery; the attached path
            # must not be invoked when target_session_id is absent.
            with patch(
                "gobby.servers.websocket.handlers.plan_approval.handle_recovered_plan_approval",
                new=AsyncMock(),
            ) as recovered:
                await handle_plan_approval_response(
                    server,
                    ws,
                    {"conversation_id": "conv-1", "decision": "approve"},
                )

        # Routing went to the conversation/recovery branch, not the attached one.
        recovered.assert_awaited_once()
        assert recovered.await_args.args[2] == "conv-1"
        attached.assert_not_awaited()
