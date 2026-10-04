"""Agent spawn never reaches tmux; tmux stays an external-pane adapter (#22856)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from gobby.agents.spawn_executor import resolve_terminal_services
from gobby.agents.spawn_models import SpawnRequest, resolve_terminal_backend
from gobby.agents.tmux.session_manager import TmuxSessionManager
from gobby.config.terminals import TerminalConfig
from gobby.dispatch._planning_enhancement import _spawn_plan_enhancer
from gobby.dispatch._rule_actions import _spawn_stage_agent
from gobby.dispatch.actions import SpawnAgentAction
from gobby.storage.terminals import TerminalManager
from gobby.terminals.runtime import PreparedSpawn, TerminalSpawnFailed, TerminalSpawnRequest
from gobby.terminals.tmux_runtime import TmuxTerminalRuntime
from tests.agents.prepared_spawn import prepared_spawn
from tests.terminals.fakes import FakeRuntime, runtime_registry

pytestmark = pytest.mark.unit

TASK_ID = "7d34e462-6ba3-5a6c-b1c6-1584b855cb83"


def test_dispatch_built_spawn_actions_request_native() -> None:
    stage = SimpleNamespace(name="planning", stage_name="planning", state="ready", position=0)
    task = SimpleNamespace(id=TASK_ID, ref="#1", additional_skills=())
    context = SimpleNamespace(prompt_context={})

    rule_action = _spawn_stage_agent(task, stage, context, "planner")
    enhancement = _spawn_plan_enhancer(task, stage, context, round_number=1, max_rounds=2)

    assert rule_action.terminal_backend == "native"
    assert enhancement.terminal_backend == "native"


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


def test_tmux_is_not_a_spawn_backend() -> None:
    with pytest.raises(ValueError, match="terminal_backend"):
        resolve_terminal_backend("tmux", None)
    with pytest.raises(ValidationError):
        TerminalConfig.model_validate({"default_backend": "tmux"})
    with pytest.raises(ValueError, match="terminal_backend"):
        SpawnAgentAction(
            task_id=TASK_ID,
            task_ref="#1",
            agent_slug="backend-developer",
            prompt="go",
            terminal_backend=cast(Any, "tmux"),
        )


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
