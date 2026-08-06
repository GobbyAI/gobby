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
        self._send_error = AsyncMock()
        self.session_manager = MagicMock()
        self.session_manager.db = MagicMock()


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
