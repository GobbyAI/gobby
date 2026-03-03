"""Tests for the set_agent WebSocket handler in SessionControlMixin."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.servers.websocket.session_control import SessionControlMixin

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class ConcreteSessionControl(SessionControlMixin):
    """Concrete implementation of SessionControlMixin for testing."""

    def __init__(self) -> None:
        self.clients: dict[Any, dict[str, Any]] = {}
        self._chat_sessions: dict[str, Any] = {}
        self._active_chat_tasks: dict[str, Any] = {}
        self._pending_modes: dict[str, str] = {}
        self._pending_worktree_paths: dict[str, str] = {}
        self._pending_agents: dict[str, str] = {}
        self._cancel_active_chat = AsyncMock()
        self._send_error = AsyncMock()
        self._fire_session_end = AsyncMock()
        self._create_chat_session = AsyncMock()
        self.session_manager = MagicMock()


def _make_ws() -> AsyncMock:
    """Create a mock websocket that records sent messages."""
    ws = AsyncMock()
    ws.send = AsyncMock()
    return ws


def _make_session(db_session_id: str | None = "db-123") -> MagicMock:
    """Create a mock ChatSession."""
    session = MagicMock()
    session.db_session_id = db_session_id
    session.stop = AsyncMock()
    return session


class TestSetAgentValidation:
    """Tests for input validation in _handle_set_agent."""

    async def test_missing_conversation_id(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        await server._handle_set_agent(ws, {"agent_name": "my-agent"})

        server._send_error.assert_awaited_once()
        assert "conversation_id" in server._send_error.call_args[0][1]

    async def test_missing_agent_name(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        await server._handle_set_agent(ws, {"conversation_id": "conv-1"})

        server._send_error.assert_awaited_once()
        assert "agent_name" in server._send_error.call_args[0][1]

    async def test_empty_data(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        await server._handle_set_agent(ws, {})

        server._send_error.assert_awaited_once()


class TestSetAgentNoExistingSession:
    """Tests for set_agent when no session exists for the conversation."""

    async def test_stores_pending_agent(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        await server._handle_set_agent(ws, {"conversation_id": "conv-1", "agent_name": "my-agent"})

        assert server._pending_agents["conv-1"] == "my-agent"

    async def test_sends_confirmation(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        await server._handle_set_agent(ws, {"conversation_id": "conv-1", "agent_name": "my-agent"})

        ws.send.assert_awaited_once()
        msg = json.loads(ws.send.call_args[0][0])
        assert msg["type"] == "agent_changed"
        assert msg["conversation_id"] == "conv-1"
        assert msg["agent_name"] == "my-agent"

    async def test_does_not_cancel_session(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        await server._handle_set_agent(ws, {"conversation_id": "conv-1", "agent_name": "my-agent"})

        server._cancel_active_chat.assert_not_awaited()


class TestSetAgentWithExistingSession:
    """Tests for set_agent when a session already exists."""

    async def test_tears_down_existing_session(self) -> None:
        server = ConcreteSessionControl()
        session = _make_session()
        server._chat_sessions["conv-1"] = session
        ws = _make_ws()

        await server._handle_set_agent(ws, {"conversation_id": "conv-1", "agent_name": "new-agent"})

        server._cancel_active_chat.assert_awaited_once_with("conv-1")
        session.stop.assert_awaited_once()
        assert "conv-1" not in server._chat_sessions

    async def test_updates_db_session_status(self) -> None:
        server = ConcreteSessionControl()
        session = _make_session(db_session_id="db-456")
        server._chat_sessions["conv-1"] = session
        ws = _make_ws()

        await server._handle_set_agent(ws, {"conversation_id": "conv-1", "agent_name": "new-agent"})

        server.session_manager.update.assert_called_once_with("db-456", status="paused")

    async def test_stores_pending_agent_after_teardown(self) -> None:
        server = ConcreteSessionControl()
        session = _make_session()
        server._chat_sessions["conv-1"] = session
        ws = _make_ws()

        await server._handle_set_agent(ws, {"conversation_id": "conv-1", "agent_name": "new-agent"})

        assert server._pending_agents["conv-1"] == "new-agent"

    async def test_sends_confirmation_after_teardown(self) -> None:
        server = ConcreteSessionControl()
        session = _make_session()
        server._chat_sessions["conv-1"] = session
        ws = _make_ws()

        await server._handle_set_agent(ws, {"conversation_id": "conv-1", "agent_name": "new-agent"})

        ws.send.assert_awaited_once()
        msg = json.loads(ws.send.call_args[0][0])
        assert msg["type"] == "agent_changed"
        assert msg["agent_name"] == "new-agent"

    async def test_no_db_session_id_skips_update(self) -> None:
        server = ConcreteSessionControl()
        session = _make_session(db_session_id=None)
        server._chat_sessions["conv-1"] = session
        ws = _make_ws()

        await server._handle_set_agent(ws, {"conversation_id": "conv-1", "agent_name": "new-agent"})

        server.session_manager.update.assert_not_called()
        # Should still succeed
        assert server._pending_agents["conv-1"] == "new-agent"

    async def test_db_update_failure_is_non_fatal(self) -> None:
        server = ConcreteSessionControl()
        session = _make_session(db_session_id="db-789")
        server._chat_sessions["conv-1"] = session
        server.session_manager.update.side_effect = RuntimeError("DB error")
        ws = _make_ws()

        # Should not raise
        await server._handle_set_agent(ws, {"conversation_id": "conv-1", "agent_name": "new-agent"})

        # Confirmation still sent
        assert server._pending_agents["conv-1"] == "new-agent"
        ws.send.assert_awaited_once()
