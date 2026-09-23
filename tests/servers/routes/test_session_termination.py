"""Tracked terminal termination through the session-expiry route."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.routes.sessions import create_sessions_router

pytestmark = pytest.mark.unit


def test_expire_session_marks_tracked_terminal_exited_before_reply() -> None:
    session_id = "session-1"
    terminal = SimpleNamespace(id="terminal-1", backend="tmux")
    session = SimpleNamespace(id=session_id, status="active", terminal_context=None)
    terminal_manager = MagicMock()
    terminal_manager.get_live_for_session.return_value = terminal
    terminal_manager.mark_exited.return_value = terminal
    runtime = MagicMock()
    runtime.terminate = AsyncMock()
    runtime_registry = MagicMock()
    runtime_registry.resolve.return_value = runtime
    server = MagicMock()
    server.services = SimpleNamespace(
        terminal_manager=terminal_manager,
        terminal_runtime_registry=runtime_registry,
    )
    server.session_manager.get.return_value = session

    async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    server.run_db = AsyncMock(side_effect=run_db)
    app = FastAPI()
    app.include_router(create_sessions_router(server))

    with patch("gobby.sessions.activity.clear_trackers") as clear_trackers:
        response = TestClient(app).post(f"/api/sessions/{session_id}/expire")

    assert response.status_code == 200
    assert response.json() == {
        "status": "expired",
        "session_id": session_id,
        "terminal_killed": True,
    }
    runtime.terminate.assert_awaited_once_with(terminal, 1.0)
    terminal_manager.mark_exited.assert_called_once_with(terminal.id)
    server.session_manager.update_status.assert_called_once_with(session_id, "expired")
    clear_trackers.assert_called_once_with(session_id)


def test_expire_session_does_not_fall_back_after_tracked_terminal_failure() -> None:
    session_id = "session-1"
    terminal = SimpleNamespace(id="terminal-1", backend="tmux")
    session = SimpleNamespace(
        id=session_id,
        status="active",
        terminal_context={"tmux_pane": "%1", "parent_pid": 123},
    )
    terminal_manager = MagicMock()
    terminal_manager.get_live_for_session.return_value = terminal
    runtime = MagicMock()
    runtime.terminate = AsyncMock(side_effect=RuntimeError("tmux kill failed"))
    runtime_registry = MagicMock()
    runtime_registry.resolve.return_value = runtime
    server = MagicMock()
    server.services = SimpleNamespace(
        terminal_manager=terminal_manager,
        terminal_runtime_registry=runtime_registry,
    )
    server.session_manager.get.return_value = session

    async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    server.run_db = AsyncMock(side_effect=run_db)
    app = FastAPI()
    app.include_router(create_sessions_router(server))

    with patch(
        "gobby.servers.routes.sessions.lifecycle.kill_terminal_session",
        new_callable=AsyncMock,
    ) as legacy_kill:
        response = TestClient(app).post(f"/api/sessions/{session_id}/expire")

    assert response.status_code == 200
    assert response.json()["terminal_killed"] is False
    legacy_kill.assert_not_awaited()
    terminal_manager.mark_exited.assert_not_called()
    server.session_manager.update_status.assert_called_once_with(session_id, "expired")
