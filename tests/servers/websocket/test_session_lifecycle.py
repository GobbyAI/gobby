"""Tests for WebSocket session lifecycle handlers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.hooks.hook_types import SessionEndReason
from gobby.servers.websocket.chat.session_registry import WebChatSessionRegistry
from gobby.servers.websocket.handlers import session_lifecycle


def _chat_session(*, stale: bool) -> SimpleNamespace:
    age = session_lifecycle.IDLE_TIMEOUT_SECONDS + 1 if stale else 0
    return SimpleNamespace(
        last_activity=datetime.now(UTC) - timedelta(seconds=age),
        db_session_id=None,
        stop=AsyncMock(),
    )


def _stop_after_one_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def sleep(_delay: float) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(session_lifecycle.asyncio, "sleep", sleep)


@pytest.mark.asyncio
async def test_idle_cleanup_preserves_session_recreated_during_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = WebChatSessionRegistry()
    stale_session = _chat_session(stale=True)
    replacement_session = _chat_session(stale=False)
    registry.register("conversation", stale_session)

    async def replace_session(_conversation_id: str) -> None:
        registry.register("conversation", replacement_session)

    mixin = SimpleNamespace(
        _chat_sessions=registry.sessions,
        _fire_session_end=AsyncMock(),
        _cancel_active_chat=AsyncMock(side_effect=replace_session),
        _session_create_locks={"conversation": asyncio.Lock()},
        web_chat_session_registry=registry,
    )
    _stop_after_one_pass(monkeypatch)

    await session_lifecycle.cleanup_idle_sessions(mixin)

    assert registry.sessions["conversation"] is replacement_session
    assert "conversation" in mixin._session_create_locks
    stale_session.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_cleanup_unregisters_registry_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = WebChatSessionRegistry()
    stale_session = _chat_session(stale=True)
    queued_compaction_task = MagicMock()
    queued_compaction_task.done.return_value = False
    queued_wake_task = MagicMock()
    queued_wake_task.done.return_value = False
    registry.register("conversation", stale_session)
    registry.active_tasks["conversation"] = MagicMock()
    registry._queued_compactions["conversation"] = ("compact", None)
    registry._queued_wakes["conversation"] = ("session", "wake")
    registry._queued_compaction_tasks["conversation"] = queued_compaction_task
    registry._queued_wake_tasks["conversation"] = queued_wake_task

    mixin = SimpleNamespace(
        _chat_sessions=registry.sessions,
        _fire_session_end=AsyncMock(),
        _cancel_active_chat=AsyncMock(),
        _session_create_locks={"conversation": asyncio.Lock()},
        web_chat_session_registry=registry,
    )
    _stop_after_one_pass(monkeypatch)

    await session_lifecycle.cleanup_idle_sessions(mixin)

    assert "conversation" not in registry.sessions
    assert "conversation" not in registry.active_tasks
    assert "conversation" not in registry._queued_compactions
    assert "conversation" not in registry._queued_wakes
    assert "conversation" not in registry._queued_compaction_tasks
    assert "conversation" not in registry._queued_wake_tasks
    assert "conversation" not in mixin._session_create_locks
    queued_compaction_task.cancel.assert_called_once_with()
    queued_wake_task.cancel.assert_called_once_with()
    stale_session.stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_idle_cleanup_uses_single_paused_lifecycle_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = WebChatSessionRegistry()
    stale_session = _chat_session(stale=True)
    stale_session.db_session_id = "db-session-123"
    registry.register("conversation", stale_session)
    session_manager = MagicMock()
    mixin = SimpleNamespace(
        _chat_sessions=registry.sessions,
        _fire_session_end=AsyncMock(),
        _cancel_active_chat=AsyncMock(),
        _session_create_locks={"conversation": asyncio.Lock()},
        web_chat_session_registry=registry,
        session_manager=session_manager,
    )
    _stop_after_one_pass(monkeypatch)

    await session_lifecycle.cleanup_idle_sessions(mixin)

    mixin._fire_session_end.assert_awaited_once_with(
        "conversation",
        reason=SessionEndReason.IDLE,
    )
    session_manager.update.assert_not_called()
    assert "conversation" not in registry.sessions


@pytest.mark.asyncio
async def test_idle_cleanup_expires_abandoned_pending_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale_time = datetime.now(UTC) - timedelta(seconds=session_lifecycle.IDLE_TIMEOUT_SECONDS + 1)
    mixin = SimpleNamespace(
        _chat_sessions={},
        _pending_modes={"abandoned": "plan"},
        _pending_projects={"abandoned": "project"},
        _pending_providers={"abandoned": "codex"},
        _pending_agents={"abandoned": "reviewer"},
        _pending_worktree_paths={"abandoned": "/tmp/worktree"},
        _pending_config_updated_at={"abandoned": stale_time},
    )
    _stop_after_one_pass(monkeypatch)

    await session_lifecycle.cleanup_idle_sessions(mixin)

    assert mixin._pending_modes == {}
    assert mixin._pending_projects == {}
    assert mixin._pending_providers == {}
    assert mixin._pending_agents == {}
    assert mixin._pending_worktree_paths == {}
    assert mixin._pending_config_updated_at == {}
