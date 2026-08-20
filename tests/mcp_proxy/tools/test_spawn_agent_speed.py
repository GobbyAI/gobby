"""Spawn-tool integration contracts for provider speed routes."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import IsolationContext
from gobby.agents.spawn_executor import execute_spawn
from gobby.agents.spawn_models import SpawnRequest, SpawnResult
from gobby.mcp_proxy.tools.spawn_agent import create_spawn_agent_registry
from gobby.providers.capabilities.models import ActivationDescriptor, SpeedMode
from gobby.providers.capabilities.resolve import SpeedResolution, SpeedStatus
from gobby.workflows.definitions import AgentDefinitionBody
from tests.agents.prepared_spawn import prepared_spawn
from tests.agents.terminal_fixtures import make_live_terminal, make_pending_terminal

pytestmark = pytest.mark.unit


def _runner() -> MagicMock:
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner.child_session_manager = MagicMock()
    runner.run_storage = MagicMock()
    runner.run_storage.has_active_run_for_task.return_value = False
    runner.run_storage.update_child_session = MagicMock()
    runner.run_storage.update_runtime = MagicMock()
    return runner


def _fast_resolution() -> SpeedResolution:
    return SpeedResolution(
        requested=SpeedMode.FAST,
        effective=SpeedMode.FAST,
        status=SpeedStatus.FAST_CONFIGURED,
        selector="droid-fast",
        activations=(ActivationDescriptor(kind="model_selector", surface="spawn-cli", params={}),),
        reason=None,
    )


async def _spawn_fast() -> tuple[dict[str, Any], SpawnRequest]:
    from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl

    runner = _runner()
    agent_body = AgentDefinitionBody(
        name="droid-worker",
        provider="droid",
        model="droid-standard",
    )

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_project_context",
            return_value={"id": "proj-abc", "project_path": "/repo"},
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_isolation_handler"
        ) as mock_get_handler,
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.resolve_spawn_speed",
            return_value=_fast_resolution(),
            create=True,
        ),
        patch("gobby.mcp_proxy.tools.spawn_agent._implementation.execute_spawn") as mock_execute,
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._implementation.get_machine_id",
            return_value="21000000-0000-4000-8000-000000000001",
        ),
    ):
        mock_handler = MagicMock()
        mock_handler.prepare_environment = AsyncMock(return_value=IsolationContext(cwd="/repo"))
        mock_handler.build_context_prompt.return_value = "Do the thing"
        mock_get_handler.return_value = mock_handler
        mock_execute.return_value = MagicMock(
            success=True,
            child_session_id="child-session-abc",
            pid=12345,
            terminal_type="tmux",
            terminal_id=None,
            tmux_socket_name=None,
            tmux_socket_path=None,
            status="running",
            message=None,
            error=None,
            process=None,
        )

        result = await spawn_agent_impl(
            prompt="Do the thing",
            runner=runner,
            agent_body=agent_body,
            speed_mode="fast",
            parent_session_id="parent-session-xyz",
        )

    return result, cast(SpawnRequest, mock_execute.call_args.args[0])


@pytest.mark.asyncio
async def test_droid_fast_selector_spawn() -> None:
    result, spawn_request = await _spawn_fast()

    assert result["success"] is True
    assert spawn_request.model == "droid-fast"
    assert spawn_request.speed_resolution == _fast_resolution()
    assert result["speed"] == {
        "requested": "fast",
        "effective": "fast",
        "status": "fast_configured",
        "reason": None,
    }


@pytest.mark.asyncio
async def test_speed_mode_not_persisted() -> None:
    _, spawn_request = await _spawn_fast()

    assert spawn_request.resume_metadata_json is not None
    assert "speed_mode" not in spawn_request.resume_metadata_json
    assert "speed" not in spawn_request.resume_metadata_json


@pytest.mark.asyncio
async def test_execute_spawn_attaches_speed_result() -> None:
    request = SpawnRequest(
        prompt="Do the thing",
        cwd="/repo",
        provider="droid",
        session_id="child-session-abc",
        run_id="run-abc",
        parent_session_id="parent-session-xyz",
        project_id="proj-abc",
        speed_resolution=_fast_resolution(),
    
    prepared_spawn=prepared_spawn(),
)
    provider_result = SpawnResult(
        success=True,
        run_id="run-abc",
        child_session_id="child-session-abc",
        status="running",
    )

    with patch(
        "gobby.agents.spawn_executor._spawn_droid_terminal",
        new=AsyncMock(return_value=provider_result),
    ):
        result = await execute_spawn(request)

    assert result.speed == {
        "requested": "fast",
        "effective": "fast",
        "status": "fast_configured",
        "reason": None,
    }


@pytest.mark.asyncio
async def test_dispatch_batch_coalesces_speed_mode() -> None:
    runner = _runner()
    registry = create_spawn_agent_registry(runner, db=MagicMock())
    agent_body = AgentDefinitionBody(name="droid-worker", provider="droid")

    with (
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory._resolve_spawn_project_context",
            return_value=({"id": "proj-abc", "project_path": "/repo"}, "/repo"),
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory._load_agent_body",
            return_value=agent_body,
        ),
        patch(
            "gobby.mcp_proxy.tools.spawn_agent._factory.spawn_agent_impl",
            new=AsyncMock(return_value={"success": True, "run_id": "run-1"}),
        ) as mock_impl,
    ):
        result = await registry.call(
            "dispatch_batch",
            {
                "suggestions": [
                    {"ref": "#1", "prompt": "Fast", "speed_mode": "fast"},
                    {"ref": "#2", "prompt": "Standard"},
                ]
            },
        )

    assert result["dispatched"] == 2
    assert [call.kwargs["speed_mode"] for call in mock_impl.await_args_list] == [
        "fast",
        "standard",
    ]
