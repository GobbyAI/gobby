"""Agent spawn never reaches tmux; tmux stays an external-pane adapter (#22856)."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from gobby.agents.spawn_executor import resolve_terminal_services
from gobby.agents.spawn_models import SpawnRequest
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.storage.terminals import TerminalManager
from gobby.terminals.runtime import PreparedSpawn, TerminalSpawnFailed, TerminalSpawnRequest
from gobby.terminals.tmux_runtime import TmuxTerminalRuntime
from tests.agents.prepared_spawn import prepared_spawn
from tests.terminals.fakes import FakeRuntime, runtime_registry

pytestmark = pytest.mark.unit


def test_resume_shaped_spawn_resolves_the_native_runtime() -> None:
    native = FakeRuntime(backend="native")
    tmux = FakeRuntime(backend="tmux")
    # Resume builds its SpawnRequest without naming a backend.
    request = SpawnRequest(
        prompt="continue",
        cwd="/tmp/work",
        provider="claude",
        session_id="child-session",
        run_id="run-1",
        parent_session_id="parent-session",
        project_id="project-1",
        prepared_spawn=prepared_spawn(),
        terminal_manager=cast(TerminalManager, object()),
        terminal_runtime_registry=runtime_registry(native, tmux),
    )

    _, _, runtime, backend = resolve_terminal_services(request)

    assert backend == "native"
    assert runtime is native


@pytest.mark.asyncio
async def test_tmux_runtime_refuses_to_spawn_without_running_tmux() -> None:
    runtime = TmuxTerminalRuntime()
    request = TerminalSpawnRequest(
        terminal_id=uuid4(),
        spawn_key="gobby-spawn",
        command=["claude"],
    )

    with patch.object(TmuxSessionManager, "_run", new=AsyncMock()) as tmux_run:
        with pytest.raises(TerminalSpawnFailed):
            await runtime.prepare_spawn(request)
        with pytest.raises(TerminalSpawnFailed):
            await runtime.commit_spawn(
                PreparedSpawn(
                    terminal_id=request.terminal_id,
                    spawn_key=request.spawn_key,
                    locator=None,
                    process=None,
                    host_terminal_id=None,
                )
            )

    tmux_run.assert_not_awaited()
