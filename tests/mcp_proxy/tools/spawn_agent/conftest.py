
"""Shared fixtures for spawn_agent tool tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gobby.workflows.definitions import AgentDefinitionBody


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
