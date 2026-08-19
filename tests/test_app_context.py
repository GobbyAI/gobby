"""Tests for ServiceContainer lazy pipeline executor creation."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.app_context import ServiceContainer

pytestmark = pytest.mark.unit


def _make_container(**overrides):
    """Create a minimal ServiceContainer with sensible defaults."""
    defaults = {
        "database": MagicMock(),
        "session_manager": MagicMock(),
        "task_manager": MagicMock(),
    }
    defaults.update(overrides)
    return ServiceContainer(**defaults)


class TestGetPipelineExecutor:
    """Tests for ServiceContainer.get_pipeline_executor()."""

    @pytest.mark.parametrize("requested_project_id", [None, "", "proj-a"])
    def test_returns_existing_executor_for_startup_project(
        self, requested_project_id: str | None
    ) -> None:
        """The startup executor handles implicit and explicit startup-project requests."""
        existing_executor = MagicMock()
        container = _make_container(
            pipeline_executor=existing_executor,
            project_id="proj-a",
        )

        result = container.get_pipeline_executor(requested_project_id)

        assert result is existing_executor

    def test_returns_none_without_workflow_loader(self) -> None:
        """Returns None when workflow_loader is unavailable."""
        container = _make_container(workflow_loader=None)

        result = container.get_pipeline_executor(project_id="proj-1")

        assert result is None

    def test_returns_none_without_database(self) -> None:
        """Returns None when database is unavailable."""
        container = _make_container(database=None, workflow_loader=MagicMock())

        result = container.get_pipeline_executor(project_id="proj-1")

        assert result is None

    def test_lazy_creation_wires_event_callback(self) -> None:
        """Lazily created executor gets event_callback wired from websocket_server."""
        mock_ws = MagicMock()
        mock_ws.broadcast_pipeline_event = AsyncMock()
        mock_db = MagicMock()
        mock_loader = MagicMock()
        mock_llm = MagicMock()

        container = _make_container(
            database=mock_db,
            workflow_loader=mock_loader,
            llm_service=mock_llm,
            websocket_server=mock_ws,
            pipeline_execution_manager=MagicMock(),
        )

        executor = container.get_pipeline_executor(project_id="proj-1")

        assert executor is not None
        assert executor.event_callback is not None

    def test_lazy_creation_wires_tool_proxy_getter(self) -> None:
        """Lazily created executor gets tool_proxy_getter wired from container."""
        mock_tool_proxy_getter = MagicMock()
        mock_db = MagicMock()
        mock_loader = MagicMock()

        container = _make_container(
            database=mock_db,
            workflow_loader=mock_loader,
            tool_proxy_getter=mock_tool_proxy_getter,
            pipeline_execution_manager=MagicMock(),
        )

        executor = container.get_pipeline_executor(project_id="proj-1")

        assert executor is not None
        assert executor.tool_proxy_getter is mock_tool_proxy_getter

    def test_lazy_creation_caches_executor(self) -> None:
        """Subsequent calls return the same cached executor."""
        container = _make_container(
            database=MagicMock(),
            workflow_loader=MagicMock(),
            pipeline_execution_manager=MagicMock(),
        )

        first = container.get_pipeline_executor(project_id="proj-1")
        second = container.get_pipeline_executor(project_id="proj-1")

        assert first is not None
        assert first is second

    def test_lazy_creation_different_projects_get_separate_executors(self) -> None:
        """Different project IDs get separate cached executors."""
        container = _make_container(
            database=MagicMock(),
            workflow_loader=MagicMock(),
            llm_service=MagicMock(),
        )

        exec_a = container.get_pipeline_executor(project_id="proj-a")
        exec_b = container.get_pipeline_executor(project_id="proj-b")

        assert exec_a is not None
        assert exec_b is not None
        assert exec_a is not exec_b

    def test_lazy_creation_without_websocket_no_event_callback(self) -> None:
        """Lazily created executor without websocket_server has no event_callback."""
        container = _make_container(
            database=MagicMock(),
            workflow_loader=MagicMock(),
            websocket_server=None,
            pipeline_execution_manager=MagicMock(),
        )

        executor = container.get_pipeline_executor(project_id="proj-1")

        assert executor is not None
        assert executor.event_callback is None

    def test_lazy_creation_without_tool_proxy_getter(self) -> None:
        """Lazily created executor without tool_proxy_getter has None."""
        container = _make_container(
            database=MagicMock(),
            workflow_loader=MagicMock(),
            tool_proxy_getter=None,
            pipeline_execution_manager=MagicMock(),
        )

        executor = container.get_pipeline_executor(project_id="proj-1")

        assert executor is not None
        assert executor.tool_proxy_getter is None

    def test_uses_container_project_id_as_fallback(self) -> None:
        """When no project_id is passed, falls back to container's project_id."""
        container = _make_container(
            database=MagicMock(),
            workflow_loader=MagicMock(),
            project_id="default-proj",
            pipeline_execution_manager=MagicMock(),
        )

        executor = container.get_pipeline_executor()

        assert executor is not None
        # Verify it was cached under the container's project_id
        assert "default-proj" in container._project_infra_cache

    def test_lazy_creation_runs_startup_sweep(self) -> None:
        """Lazily created executors sweep restart-orphaned RUNNING executions.

        Per-project executors are the only sweep point for projects outside
        the runner's home project (#17756); the sweep delegates to the
        execution manager's fail_stale_running_executions.
        """
        startup_execution_manager = MagicMock()
        project_execution_manager = MagicMock()
        project_execution_manager.fail_stale_running_executions.return_value = 0
        container = _make_container(
            database=MagicMock(),
            workflow_loader=MagicMock(),
            pipeline_execution_manager=startup_execution_manager,
            project_id="home-project",
        )

        with patch(
            "gobby.storage.pipelines.LocalPipelineExecutionManager",
            return_value=project_execution_manager,
        ):
            executor = container.get_pipeline_executor(project_id="proj-1")

        assert executor is not None
        assert executor.execution_manager is project_execution_manager
        assert container.get_pipeline_executor(project_id="proj-1") is executor
        project_execution_manager.fail_stale_running_executions.assert_called_once_with(
            exclude_ids=set()
        )
        startup_execution_manager.fail_stale_running_executions.assert_not_called()

    def test_startup_sweep_failure_does_not_block_lazy_creation(self) -> None:
        """A sweep failure must not make the executor unavailable."""
        execution_manager = MagicMock()
        execution_manager.fail_stale_running_executions.side_effect = RuntimeError("db down")
        container = _make_container(
            database=MagicMock(),
            workflow_loader=MagicMock(),
            pipeline_execution_manager=execution_manager,
        )

        executor = container.get_pipeline_executor(project_id="proj-1")

        assert executor is not None
