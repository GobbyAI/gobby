from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.servers.websocket.session_control import SessionControlMixin

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class ConcreteSessionControl(SessionControlMixin):
    def __init__(self) -> None:
        self._chat_sessions: dict[str, Any] = {}
        self._pending_worktree_paths: dict[str, str] = {}
        self._pending_projects: dict[str, str] = {}
        self.clients: dict[Any, dict[str, Any]] = {}
        self._send_error = AsyncMock()
        self.session_store = MagicMock()
        self.session_manager = self.session_store

    async def _cancel_active_chat(self, conversation_id: str) -> None:
        return None


async def test_worktree_lookup_and_path_check_are_offloaded() -> None:
    server = ConcreteSessionControl()
    websocket = AsyncMock()
    worktree = SimpleNamespace(worktree_path="/repo/worktree")

    async def run_sync(_owner: Any, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    with (
        patch("gobby.storage.worktrees.LocalWorktreeManager") as manager_class,
        patch(
            "gobby.servers.websocket.handlers.session_config.run_db",
            new_callable=AsyncMock,
            side_effect=run_sync,
        ) as run_db,
        patch(
            "gobby.servers.websocket.handlers.session_config.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value=True,
        ) as to_thread,
        patch(
            "gobby.servers.websocket.chat._session_runtime._resolve_git_branch",
            new_callable=AsyncMock,
            return_value=("feature", None),
        ),
    ):
        worktree_manager = manager_class.return_value
        worktree_manager.get.return_value = worktree
        await server._handle_set_worktree(
            websocket,
            {"conversation_id": "conversation-1", "worktree_id": "worktree-1"},
        )

    run_db.assert_awaited_once_with(server, worktree_manager.get, "worktree-1")
    to_thread.assert_awaited_once_with(os.path.isdir, "/repo/worktree")
    assert server._pending_worktree_paths == {"conversation-1": "/repo/worktree"}
    server._send_error.assert_not_awaited()
    response = json.loads(websocket.send.await_args.args[0])
    assert response == {
        "type": "worktree_switched",
        "conversation_id": "conversation-1",
        "new_branch": "feature",
        "worktree_path": "/repo/worktree",
    }


async def test_set_worktree_updates_workspace_identity_before_teardown() -> None:
    server = ConcreteSessionControl()
    websocket = AsyncMock()
    session = SimpleNamespace(db_session_id="sess-1", stop=AsyncMock())
    server._chat_sessions["conversation-1"] = session
    current = SimpleNamespace(workspace_path="/old/path", workspace_generation=3)
    store = server.session_store
    store.get.return_value = current
    update_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _update(*args: Any, **kwargs: Any) -> None:
        update_calls.append((args, kwargs))
        assert session.stop.await_count == 0

    store.update.side_effect = _update

    async def run_sync(_owner: Any, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    with (
        patch(
            "gobby.servers.websocket.handlers.session_config.run_db",
            new_callable=AsyncMock,
            side_effect=run_sync,
        ),
        patch(
            "gobby.servers.websocket.handlers.session_config.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "gobby.servers.websocket.chat._session_runtime._resolve_git_branch",
            new_callable=AsyncMock,
            return_value=("feature", None),
        ),
    ):
        await server._handle_set_worktree(
            websocket,
            {"conversation_id": "conversation-1", "worktree_path": "/repo/worktree"},
        )

    assert update_calls == [
        (
            ("sess-1",),
            {
                "status": "paused",
                "workspace_path": "/repo/worktree",
                "workspace_generation": 4,
            },
        )
    ]
    session.stop.assert_awaited_once()
    assert server._pending_worktree_paths == {"conversation-1": "/repo/worktree"}
    assert "conversation-1" not in server._chat_sessions


async def test_set_project_invalidates_workspace_identity_before_teardown() -> None:
    server = ConcreteSessionControl()
    websocket = AsyncMock()
    session = SimpleNamespace(
        db_session_id="sess-1",
        project_id="proj-old",
        stop=AsyncMock(),
    )
    server._chat_sessions["conversation-1"] = session
    current = SimpleNamespace(workspace_path="/old/path", workspace_generation=2)
    store = server.session_store
    store.get.return_value = current
    update_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def _update(*args: Any, **kwargs: Any) -> None:
        update_calls.append((args, kwargs))
        assert session.stop.await_count == 0

    store.update.side_effect = _update

    async def run_sync(_owner: Any, func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    with patch(
        "gobby.servers.websocket.handlers.session_config.run_db",
        new_callable=AsyncMock,
        side_effect=run_sync,
    ):
        await server._handle_set_project(
            websocket,
            {"conversation_id": "conversation-1", "project_id": "proj-new"},
        )

    assert update_calls == [
        (
            ("sess-1",),
            {
                "status": "paused",
                "project_id": "proj-new",
                "workspace_path": None,
                "workspace_generation": 3,
            },
        )
    ]
    session.stop.assert_awaited_once()
    assert server._pending_projects == {"conversation-1": "proj-new"}
    assert "conversation-1" not in server._chat_sessions
