"""Tests for the set_provider WebSocket handler in SessionControlMixin."""

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
        self._pending_projects: dict[str, str] = {}
        self._pending_providers: dict[str, str] = {}
        self._cancel_active_chat = AsyncMock()
        self._send_error = AsyncMock()
        self._fire_session_end = AsyncMock()
        self._create_chat_session = AsyncMock()
        self.session_manager = MagicMock()


def _make_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send = AsyncMock()
    return ws


def _make_session(provider: str = "claude", db_session_id: str | None = "db-123") -> MagicMock:
    session = MagicMock()
    session.provider = provider
    session.db_session_id = db_session_id
    session.stop = AsyncMock()
    return session


class TestSetProviderValidation:
    async def test_missing_conversation_id(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        await server._handle_set_provider(ws, {"provider": "gemini"})

        server._send_error.assert_awaited_once()
        assert "conversation_id" in server._send_error.call_args[0][1]

    async def test_missing_provider(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        await server._handle_set_provider(ws, {"conversation_id": "conv-1"})

        server._send_error.assert_awaited_once()
        assert "provider" in server._send_error.call_args[0][1]

    async def test_invalid_provider(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        await server._handle_set_provider(
            ws, {"conversation_id": "conv-1", "provider": "bad-provider"}
        )

        server._send_error.assert_awaited_once()
        assert "Invalid provider" in server._send_error.call_args[0][1]


class TestSetProviderNoExistingSession:
    async def test_stores_pending_provider(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        await server._handle_set_provider(ws, {"conversation_id": "conv-1", "provider": "gemini"})

        assert server._pending_providers["conv-1"] == "gemini"

    async def test_sends_confirmation(self) -> None:
        server = ConcreteSessionControl()
        ws = _make_ws()

        await server._handle_set_provider(ws, {"conversation_id": "conv-1", "provider": "codex"})

        ws.send.assert_awaited_once()
        msg = json.loads(ws.send.call_args[0][0])
        assert msg["type"] == "provider_switched"
        assert msg["conversation_id"] == "conv-1"
        assert msg["provider"] == "codex"
        assert msg["old_provider"] is None


class TestSetProviderWithExistingSession:
    async def test_tears_down_existing_session(self) -> None:
        server = ConcreteSessionControl()
        session = _make_session(provider="claude")
        server._chat_sessions["conv-1"] = session
        ws = _make_ws()

        await server._handle_set_provider(ws, {"conversation_id": "conv-1", "provider": "gemini"})

        server._cancel_active_chat.assert_awaited_once_with("conv-1")
        session.stop.assert_awaited_once()
        assert "conv-1" not in server._chat_sessions

    async def test_updates_db_session_status(self) -> None:
        server = ConcreteSessionControl()
        session = _make_session(provider="claude", db_session_id="db-456")
        server._chat_sessions["conv-1"] = session
        ws = _make_ws()

        await server._handle_set_provider(ws, {"conversation_id": "conv-1", "provider": "gemini"})

        server.session_manager.update.assert_called_once_with("db-456", status="paused")

    async def test_stores_pending_provider_after_teardown(self) -> None:
        server = ConcreteSessionControl()
        session = _make_session(provider="claude")
        server._chat_sessions["conv-1"] = session
        ws = _make_ws()

        await server._handle_set_provider(ws, {"conversation_id": "conv-1", "provider": "codex"})

        assert server._pending_providers["conv-1"] == "codex"

    async def test_sends_old_provider_in_confirmation(self) -> None:
        server = ConcreteSessionControl()
        session = _make_session(provider="claude")
        server._chat_sessions["conv-1"] = session
        ws = _make_ws()

        await server._handle_set_provider(ws, {"conversation_id": "conv-1", "provider": "gemini"})

        msg = json.loads(ws.send.call_args[0][0])
        assert msg["old_provider"] == "claude"
        assert msg["provider"] == "gemini"
