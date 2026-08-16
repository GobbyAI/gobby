"""Shared fixtures for spawn_agent tool tests."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from gobby.agents.isolation import IsolationContext
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.workflows.definitions import AgentDefinitionBody


@pytest.fixture(autouse=True)
def _mock_spawn_machine_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep spawn tests isolated from the user's machine-ID file."""
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.get_machine_id",
        lambda: "21000000-0000-4000-8000-000000000004",
    )


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> AgentDefinitionManager:
    return AgentDefinitionManager(db)


@pytest.fixture
def mock_runner() -> MagicMock:
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner._child_session_manager = MagicMock()
    runner.run_storage.has_active_run_for_task.return_value = False
    runner.agent_lifecycle_monitor = None
    runner.task_manager = None

    def cancel_run(run_id: str) -> bool:
        return runner.run_storage.cancel(run_id) is not None

    runner.cancel_run.side_effect = cancel_run
    return runner


@pytest.fixture
def agent_body() -> AgentDefinitionBody:
    return AgentDefinitionBody(
        name="default",
        provider="claude",
    )


@pytest.fixture
def isolation_context() -> IsolationContext:
    return IsolationContext(cwd="/path/to/project")


@pytest.fixture
def build_agent_body() -> Callable[..., AgentDefinitionBody]:
    def _build_agent_body(**overrides: object) -> AgentDefinitionBody:
        defaults: dict[str, object] = {
            "name": "default",
            "provider": "claude",
        }
        defaults.update(overrides)
        return AgentDefinitionBody(**defaults)

    return _build_agent_body
