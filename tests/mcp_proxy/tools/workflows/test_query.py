"""Tests for workflow query tools — get_step_status and pipeline discovery."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.workflows.step_instances import AgentStepInstance
from tests.workflows.step_instance_fixtures import make_step_instance

pytestmark = pytest.mark.unit


def _make_mocks(
    instance: AgentStepInstance | None = None,
    session_variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create mock dependencies for query functions."""
    session_manager = MagicMock()
    session_manager.resolve_session_reference.return_value = "uuid-session-1"

    instance_manager = MagicMock()
    instance_manager.get_for_session.return_value = instance

    session_var_manager = MagicMock()
    session_var_manager.get_variables.return_value = session_variables or {}

    return {
        "session_manager": session_manager,
        "instance_manager": instance_manager,
        "session_var_manager": session_var_manager,
    }


class TestGetStepStatus:
    """Tests for get_step_status against the typed instance."""

    def test_returns_snapshot_status(self) -> None:
        from gobby.mcp_proxy.tools.workflows._query import get_step_status

        instance = make_step_instance(
            "uuid-session-1",
            agent_name="auto-task",
            current_step="work",
            variables={"session_task": "task-uuid"},
            steps=["work", "done"],
        )
        mocks = _make_mocks(instance=instance, session_variables={"counter": 5})

        result = get_step_status(
            mocks["session_manager"],
            session_id="#1",
            instance_manager=mocks["instance_manager"],
            session_var_manager=mocks["session_var_manager"],
        )

        assert result["success"] is True
        assert result["has_workflow"] is True
        assert result["agent_name"] == "auto-task"
        assert result["current_step"] == "work"
        assert result["steps"] == ["work", "done"]
        assert result["variables"] == {"session_task": "task-uuid"}
        assert result["session_variables"] == {"counter": 5}

    def test_shows_session_variables_separately(self) -> None:
        from gobby.mcp_proxy.tools.workflows._query import get_step_status

        instance = make_step_instance(
            "uuid-session-1",
            agent_name="auto-task",
            current_step="work",
        )
        mocks = _make_mocks(
            instance=instance,
            session_variables={"shared_flag": True, "counter": 42},
        )

        result = get_step_status(
            mocks["session_manager"],
            session_id="#1",
            instance_manager=mocks["instance_manager"],
            session_var_manager=mocks["session_var_manager"],
        )

        assert result["success"] is True
        assert result["session_variables"] == {"shared_flag": True, "counter": 42}

    def test_no_instance_manager_returns_no_workflows(self) -> None:
        from gobby.mcp_proxy.tools.workflows._query import get_step_status

        mocks = _make_mocks()

        result = get_step_status(
            mocks["session_manager"],
            session_id="#1",
        )

        assert result["success"] is True
        assert result["has_workflow"] is False

    def test_empty_instance_returns_no_workflows(self) -> None:
        from gobby.mcp_proxy.tools.workflows._query import get_step_status

        mocks = _make_mocks(instance=None)

        result = get_step_status(
            mocks["session_manager"],
            session_id="#1",
            instance_manager=mocks["instance_manager"],
            session_var_manager=mocks["session_var_manager"],
        )

        assert result["success"] is True
        assert result["has_workflow"] is False


def test_query_module_keeps_step_status_only() -> None:
    from gobby.mcp_proxy.tools.workflows import _query

    assert hasattr(_query, "get_step_status")
    assert not hasattr(_query, "list_workflows")
    assert not hasattr(_query, "get_workflow")
    assert not hasattr(_query, "get_workflow_status")


class TestPipelineDiscoverySurface:
    """list_pipelines is the surviving DB/filesystem discovery surface."""

    @pytest.mark.asyncio
    async def test_list_pipelines_returns_discovered_definitions(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipeline_discovery import list_pipelines

        discovered = [
            SimpleNamespace(
                name="db-pipe",
                is_project=False,
                path="/db/db-pipe",
                priority=100,
                definition=SimpleNamespace(description="from db", steps=[object()]),
            ),
            SimpleNamespace(
                name="fs-pipe",
                is_project=True,
                path="/proj/.gobby/workflows/pipelines/fs-pipe.yaml",
                priority=50,
                definition=SimpleNamespace(description="from filesystem", steps=[]),
            ),
        ]
        loader = MagicMock()
        loader.discover_pipelines = AsyncMock(return_value=discovered)

        result = await list_pipelines(loader, project_id="proj")

        assert result["success"] is True
        assert result["count"] == 2
        names = [item["name"] for item in result["pipelines"]]
        assert names == ["db-pipe", "fs-pipe"]
        assert result["pipelines"][0]["description"] == "from db"
        assert result["pipelines"][1]["is_project"] is True
        loader.discover_pipelines.assert_awaited_once_with("proj")
