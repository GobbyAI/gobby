"""A settled terminal exit must not hide a still-running agent from the watchdog."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.mcp_proxy.tools.agents import create_agents_registry
from gobby.mcp_proxy.tools.spawn_agent._spawn_guards import agent_slot_cap_refusal
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import session_context_for_test
from tests.agents.test_lifecycle_monitor import (
    DETECTION_REGISTRY,
    LOCAL_MACHINE_ID,
    LifecycleRuntime,
    _fake_terminal_services,
    _make_terminal_run,
)
from tests.fixtures.isolated_checkout import patch_local_machine_id

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["native", "tmux"])
@pytest.mark.parametrize("registry_lost", [False, True])
async def test_exit_before_first_turn_releases_slot_and_delivers_parent_wait(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    backend: Literal["native", "tmux"],
    registry_lost: bool,
) -> None:
    patch_local_machine_id(monkeypatch, LOCAL_MACHINE_ID)
    parent = session_manager.register(
        external_id=f"exit-parent-{uuid4()}",
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=sample_project["id"],
    )
    child = session_manager.register(
        external_id=f"exit-child-{uuid4()}",
        machine_id=LOCAL_MACHINE_ID,
        source="qwen",
        project_id=sample_project["id"],
        parent_session_id=parent.id,
        agent_depth=1,
    )
    runs = LocalAgentRunManager(temp_db)
    runtime = LifecycleRuntime(backend=backend)
    services = _fake_terminal_services(temp_db, runtime)
    run = _make_terminal_run(
        runs,
        parent.to_dict(),
        run_id=str(uuid4()),
        child_session_id=child.id,
        provider="qwen",
        backend=backend,
    )
    assert run.terminal_id is not None
    wakes: list[tuple[str, dict[str, Any]]] = []

    async def wake(session_id: str, _message: str, result: dict[str, Any]) -> dict[str, bool]:
        wakes.append((session_id, result))
        return {"ism_persisted": True}

    completion = CompletionEventRegistry(wake_callback=wake)
    runner = MagicMock()
    runner.run_storage = runs
    runner.get_run.side_effect = runs.get
    runner.terminal_runtime_registry = services.registry
    agents = create_agents_registry(
        runner,
        db=temp_db,
        session_manager=session_manager,
        completion_registry=completion,
    )
    monitor = AgentLifecycleMonitor(
        agent_run_manager=runs,
        db=temp_db,
        detection_registry=DETECTION_REGISTRY,
        session_manager=session_manager,
        completion_registry=completion,
        terminal_services=services,
    )
    with session_context_for_test(parent.id):
        waiting = await agents.call("wait_for_agent", {"run_id": run.id})
    assert waiting["completed"] is False
    assert waiting["notification_registered"] is True
    assert await monitor.check_unhealthy_agents() == 0
    assert wakes == []

    # Losing the in-memory registry must preserve the durable parent subscription.
    if registry_lost:
        completion.cleanup(run.id)
    terminal = services.manager.mark_exited(run.terminal_id)
    assert terminal is not None and terminal.state == "exited"
    assert services.terminal_for(run) is None

    with patch(
        "gobby.mcp_proxy.tools.spawn_agent._spawn_guards.max_active_agents_for_project",
        return_value=1,
    ):
        slot_args = {
            "project_id": str(sample_project["id"]),
            "project_path": str(tmp_path),
            "caller_session_id": parent.id,
        }
        assert agent_slot_cap_refusal(temp_db, **slot_args) is not None
        # One scheduled health pass detects the exit; no turn or timeout is required.
        assert await asyncio.wait_for(monitor.check_unhealthy_agents(), timeout=10) == 1
        assert agent_slot_cap_refusal(temp_db, **slot_args) is None

    stored = runs.get(run.id)
    assert stored is not None
    assert stored.status == "error"
    assert stored.turns_used == 0
    assert stored.tool_calls_count == 0
    assert "terminal session died unexpectedly" in (stored.error or "")
    assert stored.capture_id is not None
    assert wakes == [
        (
            parent.id,
            {
                "status": "error",
                "error": stored.error,
                "run_id": run.id,
                "completion_id": run.id,
            },
        )
    ]
    with session_context_for_test(parent.id):
        finished = await agents.call("wait_for_agent", {"run_id": run.id})
        result = await agents.call("get_agent_result", {"run_id": run.id})
    assert finished["completed"] is True
    assert result["status"] == "error"
    assert result["live_output"]["available"] is False
    assert await monitor.check_unhealthy_agents() == 0
    assert len(wakes) == 1
