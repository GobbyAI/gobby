"""Acceptance 2.5.11: web terminal_create uses the row-owning spawn primitive."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.terminals.web_spawn import spawn_web_terminal
from tests.storage.test_terminals import LOCAL_MACHINE_ID, _manager

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_web_create_uses_row_owning_primitive(
    temp_db: HubDatabase, sample_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID)
    manager = _manager(temp_db)
    runtime = MagicMock()
    runtime.backend = "tmux"
    runtime.prepare_spawn = AsyncMock(
        return_value=MagicMock(
            stored_locator={
                "socket_path": "/tmp/tmux/default",
                "server_pid": 1,
                "server_start_time": 2,
                "pane_id": "%1",
            },
            locator_key="tmux:/tmp/tmux/default:1:2:%1",
            process=None,
            acknowledge_persist=MagicMock(),
        )
    )
    runtime.commit_spawn = AsyncMock(
        return_value=MagicMock(locator=MagicMock(frame_host_epoch=None))
    )
    runtime.terminate = AsyncMock()
    result = await spawn_web_terminal(
        manager=manager,
        runtime=runtime,
        project_id=sample_project["id"],
        session_id=None,
        rows=24,
        cols=80,
        cwd="/tmp",
        command=["zsh"],
    )
    assert result.success is True
    row = manager.get(result.terminal_id)
    assert row is not None
    assert row.ownership == "gobby"
    assert row.state == "live"
    listing = manager.list_by_project(sample_project["id"])
    assert row.id in {item.id for item in listing}

    runtime.prepare_spawn = AsyncMock(side_effect=Exception("backend boom"))
    failed = await spawn_web_terminal(
        manager=manager,
        runtime=runtime,
        project_id=sample_project["id"],
        session_id=None,
        rows=24,
        cols=80,
        cwd="/tmp",
        command=["zsh"],
    )
    assert failed.success is False
    failed_row = manager.get(failed.terminal_id)
    assert failed_row is not None
    assert failed_row.state == "exited"
