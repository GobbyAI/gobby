"""Shared fixtures for spawn_agent tool tests."""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from gobby.agents.isolation import IsolationContext
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.workflows.definitions import AgentDefinitionBody


@pytest.fixture
def db(temp_db: HubDatabase) -> HubDatabase:
    database = temp_db
    return database


@pytest.fixture
def manager(db: HubDatabase) -> LocalWorkflowDefinitionManager:
    return LocalWorkflowDefinitionManager(db)


@pytest.fixture
def mock_runner() -> MagicMock:
    runner = MagicMock()
    runner.can_spawn.return_value = (True, "Can spawn", 0)
    runner._child_session_manager = MagicMock()
    runner.run_storage.has_active_run_for_task.return_value = False
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
