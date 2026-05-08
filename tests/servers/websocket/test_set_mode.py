"""Tests for the set_mode WebSocket handler in SessionControlMixin."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.websocket.session_control import SessionControlMixin

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class ConcreteSessionControl(SessionControlMixin):
    def __init__(self) -> None:
        self.clients: dict[Any, dict[str, Any]] = {}
        self._chat_sessions: dict[str, Any] = {}
        self._active_chat_tasks: dict[str, Any] = {}
        self._pending_modes: dict[str, str] = {}
        self._pending_worktree_paths: dict[str, str] = {}
        self._pending_agents: dict[str, str] = {}
        self._pending_projects: dict[str, str] = {}
        self._pending_providers: dict[str, str] = {}
        self._cancel_active_chat = AsyncMock()
        self._send_error = AsyncMock()
        self._fire_session_end = AsyncMock()
        self._create_chat_session = AsyncMock()
        self.session_manager = MagicMock()
        self.session_manager.db = MagicMock()


def _make_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send = AsyncMock()
    return ws


def _make_session(chat_mode: str = "normal", db_session_id: str | None = "db-123") -> MagicMock:
    session = MagicMock()
    session.chat_mode = chat_mode
    session.db_session_id = db_session_id
    session.has_pending_plan = False
    session.sync_sdk_permission_mode = AsyncMock()
    return session


class TestSetModeValidation:
    async def test_invalid_mode_sends_error(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        await server._handle_set_mode(ws, {"conversation_id": "conv-1", "mode": "bogus"})

        server._send_error.assert_awaited_once()
        assert "Invalid mode" in server._send_error.call_args[0][1]


class TestSetModeNoExistingSession:
    async def test_queues_mode_for_future_session(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        await server._handle_set_mode(ws, {"conversation_id": "conv-1", "mode": "plan"})

        assert server._pending_modes["conv-1"] == "plan"


class TestSetModeIdempotency:
    async def test_plan_to_plan_is_noop(self) -> None:
        server = ConcreteSessionControl()
        session = _make_session(chat_mode="plan")
        server._chat_sessions["conv-1"] = session
        ws = _make_ws()

        with patch("gobby.workflows.state_manager.SessionVariableManager") as svm_cls:
            await server._handle_set_mode(ws, {"conversation_id": "conv-1", "mode": "plan"})

        session.set_chat_mode.assert_not_called()
        assert session.set_chat_mode.call_count == 0
        assert not session.set_chat_mode.called
        session.sync_sdk_permission_mode.assert_not_awaited()
        assert session.sync_sdk_permission_mode.await_count == 0
        assert session.sync_sdk_permission_mode.await_args is None
        svm_cls.assert_not_called()
        assert svm_cls.call_count == 0
        assert not svm_cls.called

    async def test_accept_edits_noop_when_already_normal(self) -> None:
        """accept_edits is normalized to normal, so if session is already normal the handler should no-op."""
        server = ConcreteSessionControl()
        session = _make_session(chat_mode="normal")
        server._chat_sessions["conv-1"] = session
        ws = _make_ws()

        with patch("gobby.workflows.state_manager.SessionVariableManager") as svm_cls:
            await server._handle_set_mode(ws, {"conversation_id": "conv-1", "mode": "accept_edits"})

        session.set_chat_mode.assert_not_called()
        assert session.set_chat_mode.call_count == 0
        assert not session.set_chat_mode.called
        session.sync_sdk_permission_mode.assert_not_awaited()
        assert session.sync_sdk_permission_mode.await_count == 0
        assert session.sync_sdk_permission_mode.await_args is None
        svm_cls.assert_not_called()
        assert svm_cls.call_count == 0
        assert not svm_cls.called

    async def test_real_change_fires_full_pipeline(self) -> None:
        server = ConcreteSessionControl()
        session = _make_session(chat_mode="normal")
        server._chat_sessions["conv-1"] = session
        ws = _make_ws()

        with patch("gobby.workflows.state_manager.SessionVariableManager") as svm_cls:
            svm_instance = MagicMock()
            svm_cls.return_value = svm_instance
            await server._handle_set_mode(ws, {"conversation_id": "conv-1", "mode": "plan"})

        session.set_chat_mode.assert_called_once_with("plan")
        session.sync_sdk_permission_mode.assert_awaited_once()
        svm_instance.merge_variables.assert_called_once()
        merged_vars = svm_instance.merge_variables.call_args[0][1]
        assert merged_vars["chat_mode"] == "plan"


class TestSetModeAttachedSession:
    """Verify set_mode with target_session_id drives an attached session via storage."""

    async def test_target_session_id_routes_to_storage_update(self) -> None:
        server = ConcreteSessionControl()
        attached_session = MagicMock()
        attached_session.chat_mode = "normal"
        server.session_manager.get = MagicMock(return_value=attached_session)
        ws = _make_ws()

        with patch("gobby.workflows.state_manager.SessionVariableManager") as svm_cls:
            svm_instance = MagicMock()
            svm_cls.return_value = svm_instance
            await server._handle_set_mode(
                ws,
                {"target_session_id": "tmux-uuid-1", "mode": "plan"},
            )

        server.session_manager.update_chat_mode.assert_called_once_with("tmux-uuid-1", "plan")
        svm_instance.merge_variables.assert_called_once()
        merged_vars = svm_instance.merge_variables.call_args[0][1]
        assert merged_vars["chat_mode"] == "plan"

    async def test_target_session_id_no_change_skips_update(self) -> None:
        server = ConcreteSessionControl()
        attached_session = MagicMock()
        attached_session.chat_mode = "plan"
        server.session_manager.get = MagicMock(return_value=attached_session)
        ws = _make_ws()

        await server._handle_set_mode(
            ws,
            {"target_session_id": "tmux-uuid-1", "mode": "plan"},
        )

        server.session_manager.update_chat_mode.assert_not_called()

    async def test_target_session_not_found_returns_error(self) -> None:
        server = ConcreteSessionControl()
        server.session_manager.get = MagicMock(return_value=None)
        ws = _make_ws()

        await server._handle_set_mode(
            ws,
            {"target_session_id": "missing-uuid", "mode": "plan"},
        )

        server._send_error.assert_awaited_once()
        server.session_manager.update_chat_mode.assert_not_called()
