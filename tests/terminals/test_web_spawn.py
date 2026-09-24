"""What a daemon-spawned terminal records about itself (gclient-chrome-refresh 2.2)."""

from __future__ import annotations

from typing import cast

import pytest

from gobby.storage.terminals import TerminalManager
from gobby.terminals.runtime import TerminalRuntime
from gobby.terminals.web_spawn import spawn_web_terminal
from tests.terminals.fakes import FakeRuntime, MemoryTerminalStore

pytestmark = pytest.mark.unit


async def test_spawn_records_the_shell_basename_in_process() -> None:
    manager = MemoryTerminalStore()

    result = await spawn_web_terminal(
        manager=cast(TerminalManager, manager),
        runtime=cast(TerminalRuntime, FakeRuntime(backend="native")),
        project_id="project-1",
        session_id=None,
        rows=24,
        cols=80,
        cwd="/tmp",
        command=["/bin/zsh", "-l"],
    )

    assert result.success is True
    row = manager.get(result.terminal_id)
    assert row is not None
    assert row.process is not None
    assert row.process == {"host_terminal_id": "ht-1", "shell": "zsh"}
