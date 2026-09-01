"""Integration tests for agent lifecycle contracts pinned by storage state."""

from __future__ import annotations

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.fixtures.isolated_checkout import patch_local_machine_id

pytestmark = pytest.mark.integration

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_local_machine_id(monkeypatch, LOCAL_MACHINE_ID)


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
        machine_id="21000000-0000-4000-8000-000000000001",
        source="claude",
        project_id=sample_project["id"],
    )
    return session.to_dict()


@pytest.mark.asyncio
async def test_start_after_terminal_run_is_noop(
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
