"""Integration tests for agent lifecycle contracts pinned by storage state."""

from __future__ import annotations

import signal
from unittest.mock import AsyncMock, patch

import pytest

from gobby.agents.registry import RunningAgent, RunningAgentRegistry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.integration


@pytest.fixture
def agent_manager(temp_db: HubDatabase) -> LocalAgentRunManager:
    return LocalAgentRunManager(temp_db)


@pytest.fixture
def sample_session(
    session_manager: SessionManager,
    sample_project: dict,
) -> dict:
    session = session_manager.register(
        external_id="agent-lifecycle-contract",
        machine_id="machine-1",
        source="claude",
        project_id=sample_project["id"],
    )
    return session.to_dict()


def test_start_after_terminal_run_is_noop(
    agent_manager: LocalAgentRunManager,
    sample_session: dict,
) -> None:
    agent_run = agent_manager.create(
        parent_session_id=sample_session["id"],
        provider="claude",
        prompt="terminal run",
    )

    terminal = agent_manager.complete(agent_run.id, result="done")
    assert terminal is not None
    assert terminal.status == "success"

    restarted = agent_manager.start(agent_run.id)

    assert restarted is None
    reloaded = agent_manager.get(agent_run.id)
    assert reloaded is not None
    assert reloaded.status == "success"
    assert reloaded.started_at is None
    assert reloaded.completed_at == terminal.completed_at


async def test_kill_keeps_run_registered_until_process_signal_succeeds(
    agent_manager: LocalAgentRunManager,
    sample_session: dict,
) -> None:
    agent_run = agent_manager.create(
        parent_session_id=sample_session["id"],
        provider="claude",
        prompt="kill contract",
    )
    started = agent_manager.start(agent_run.id)
    assert started is not None

    registry = RunningAgentRegistry()
    registry.add(
        RunningAgent(
            run_id=agent_run.id,
            session_id="child-session",
            parent_session_id=sample_session["id"],
            pid=12345,
            provider="claude",
        )
    )

    call_count = 0

    def fake_kill(pid: int, sig: int) -> None:
        nonlocal call_count
        assert pid == 12345
        assert registry.get(agent_run.id) is not None
        stored = agent_manager.get(agent_run.id)
        assert stored is not None
        assert stored.status == "running"
        if sig == 0 and call_count == 2:
            raise ProcessLookupError
        call_count += 1

    with (
        patch("os.kill", side_effect=fake_kill) as mock_kill,
        patch(
            "gobby.agents.registry.RunningAgentRegistry._run_subprocess",
            new_callable=AsyncMock,
            return_value=(0, "claude session-id child-session", ""),
        ),
    ):
        result = await registry.kill(agent_run.id)

    assert result["success"] is True
    mock_kill.assert_any_call(12345, signal.SIGTERM)
    assert registry.get(agent_run.id) is None
    stored_after_kill = agent_manager.get(agent_run.id)
    assert stored_after_kill is not None
    assert stored_after_kill.status == "running"
