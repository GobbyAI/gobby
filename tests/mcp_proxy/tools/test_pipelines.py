"""Tests for pipelines MCP tools.

TDD tests for the pipelines MCP registry and tools.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.loader_cache import DiscoveredWorkflow

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_loader() -> MagicMock:
    """Create a mock workflow loader."""
    loader = MagicMock()
    loader.discover_pipeline_workflows = AsyncMock(return_value=[])
    loader.discover_pipeline_workflows_sync.return_value = []
    loader.load_pipeline = AsyncMock()
    return loader


@pytest.fixture
def mock_execution_manager() -> MagicMock:
    """Create a mock execution manager."""
    manager = MagicMock()
    return manager


@pytest.fixture
def mock_executor(mock_execution_manager: MagicMock) -> MagicMock:
    """Create a mock pipeline executor with execution_manager attached."""
    from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

    executor = MagicMock()
    # run_pipeline accesses executor.execution_manager.create_execution()
    executor.execution_manager = mock_execution_manager
    mock_execution_manager.create_execution.return_value = PipelineExecution(
        id="pe-abc123",
        pipeline_name="default",
        project_id="",
        status=ExecutionStatus.PENDING,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:00Z",
    )
    return executor


class TestCreatePipelinesRegistry:
    """Tests for create_pipelines_registry function."""

    def test_returns_internal_tool_registry(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that create_pipelines_registry returns an InternalToolRegistry."""
        from gobby.mcp_proxy.tools.internal import InternalToolRegistry
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        assert isinstance(registry, InternalToolRegistry)

    def test_registry_has_list_pipelines_tool(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that list_pipelines tool is registered."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        tools = registry.list_tools()
        tool_names = [t["name"] for t in tools]

        assert "list_pipelines" in tool_names

    def test_registry_name(self, mock_loader, mock_executor, mock_execution_manager) -> None:
        """Test that registry has correct name."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        assert registry.name == "gobby-workflows"


class TestListPipelinesTool:
    """Tests for the list_pipelines MCP tool."""

    @pytest.mark.asyncio
    async def test_list_pipelines_calls_discover(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that list_pipelines calls loader.discover_pipeline_workflows()."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        mock_loader.discover_pipeline_workflows.return_value = []

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        # Reset mock after registry creation (which also calls discover for dynamic tools)
        mock_loader.discover_pipeline_workflows.reset_mock()

        # Call the tool
        await registry.call("list_pipelines", {})

        mock_loader.discover_pipeline_workflows.assert_called_once()

    @pytest.mark.asyncio
    async def test_list_pipelines_returns_pipeline_info(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that list_pipelines returns pipeline names and descriptions."""
        from pathlib import Path

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        # Create mock discovered pipelines
        pipeline1 = PipelineDefinition(
            name="deploy",
            description="Deploy to production",
            steps=[PipelineStep(id="step1", exec="echo deploy")],
        )
        pipeline2 = PipelineDefinition(
            name="test",
            description="Run test suite",
            steps=[PipelineStep(id="step1", exec="pytest")],
        )

        mock_loader.discover_pipeline_workflows.return_value = [
            DiscoveredWorkflow(
                name="deploy",
                definition=pipeline1,
                priority=100,
                is_project=False,
                path=Path("/workflows/deploy.yaml"),
            ),
            DiscoveredWorkflow(
                name="test",
                definition=pipeline2,
                priority=100,
                is_project=True,
                path=Path("/project/.gobby/workflows/test.yaml"),
            ),
        ]

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call("list_pipelines", {})

        assert result["success"] is True
        assert "pipelines" in result
        assert len(result["pipelines"]) == 2

        # Check pipeline info
        names = [p["name"] for p in result["pipelines"]]
        assert "deploy" in names
        assert "test" in names

        # Check descriptions are included
        deploy = next(p for p in result["pipelines"] if p["name"] == "deploy")
        assert deploy["description"] == "Deploy to production"

    @pytest.mark.asyncio
    async def test_list_pipelines_includes_is_project(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that list_pipelines indicates if pipeline is project-specific."""
        from pathlib import Path

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        pipeline = PipelineDefinition(
            name="local",
            steps=[PipelineStep(id="step1", exec="echo local")],
        )

        mock_loader.discover_pipeline_workflows.return_value = [
            DiscoveredWorkflow(
                name="local",
                definition=pipeline,
                priority=100,
                is_project=True,
                path=Path("/project/.gobby/workflows/local.yaml"),
            ),
        ]

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call("list_pipelines", {})

        assert result["pipelines"][0]["is_project"] is True

    @pytest.mark.asyncio
    async def test_list_pipelines_with_project_path(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that list_pipelines passes project_path to discover."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        mock_loader.discover_pipeline_workflows.return_value = []

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        # Reset mock after registry creation (which also calls discover for dynamic tools)
        mock_loader.discover_pipeline_workflows.reset_mock()

        await registry.call("list_pipelines", {"project_path": "/my/project"})

        mock_loader.discover_pipeline_workflows.assert_called_once_with(project_path="/my/project")

    @pytest.mark.asyncio
    async def test_list_pipelines_empty_result(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that list_pipelines handles no pipelines found."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        mock_loader.discover_pipeline_workflows.return_value = []

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call("list_pipelines", {})

        assert result["success"] is True
        assert result["pipelines"] == []


class TestRunPipelineTool:
    """Tests for the run_pipeline MCP tool."""

    def test_registry_has_run_pipeline_tool(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that run_pipeline tool is registered."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        tools = registry.list_tools()
        tool_names = [t["name"] for t in tools]

        assert "run_pipeline" in tool_names

    @pytest.mark.asyncio
    async def test_run_pipeline_loads_pipeline(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that run_pipeline loads the pipeline via loader."""
        from unittest.mock import AsyncMock

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        pipeline = PipelineDefinition(
            name="deploy",
            steps=[PipelineStep(id="step1", exec="echo deploy")],
        )
        mock_loader.load_pipeline.return_value = pipeline

        # Mock executor.execute as async
        execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.COMPLETED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            outputs_json='{"result": "success"}',
        )
        mock_executor.execute = AsyncMock(return_value=execution)

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        await registry.call(
            "run_pipeline",
            {"name": "deploy", "inputs": {}, "session_id": "sess-1"},
        )

        mock_loader.load_pipeline.assert_called_once_with("deploy")

    @pytest.mark.asyncio
    async def test_run_pipeline_calls_executor(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that run_pipeline creates execution and launches background task."""
        from unittest.mock import AsyncMock

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        pipeline = PipelineDefinition(
            name="deploy",
            steps=[PipelineStep(id="step1", exec="echo deploy")],
        )
        mock_loader.load_pipeline.return_value = pipeline
        mock_executor.execute = AsyncMock(return_value=MagicMock())

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call(
            "run_pipeline",
            {"name": "deploy", "inputs": {"env": "prod"}, "session_id": "sess-1"},
        )

        # Execution record pre-created
        mock_execution_manager.create_execution.assert_called_once()
        assert result["success"] is True
        assert result["status"] == "running"
        assert result["execution_id"] == "pe-abc123"

    @pytest.mark.asyncio
    async def test_run_pipeline_returns_running_status(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that run_pipeline returns running status immediately (fire-and-forget)."""
        from unittest.mock import AsyncMock

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        pipeline = PipelineDefinition(
            name="deploy",
            steps=[PipelineStep(id="step1", exec="echo deploy")],
        )
        mock_loader.load_pipeline.return_value = pipeline
        mock_executor.execute = AsyncMock(return_value=MagicMock())

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call(
            "run_pipeline",
            {"name": "deploy", "inputs": {}, "session_id": "sess-1"},
        )

        assert result["success"] is True
        assert result["status"] == "running"
        assert result["execution_id"] == "pe-abc123"
        assert "notified" in result["message"].lower() or "started" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_run_pipeline_returns_running_even_if_approval_needed(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that run_pipeline returns running status even when approval will be required.

        Since execution is fire-and-forget, the approval pause happens in the
        background. The caller should poll get_pipeline_status to discover it.
        """
        from unittest.mock import AsyncMock

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry
        from gobby.workflows.pipeline_state import ApprovalRequired

        pipeline = PipelineDefinition(
            name="deploy",
            steps=[PipelineStep(id="step1", exec="echo deploy")],
        )
        mock_loader.load_pipeline.return_value = pipeline

        # Executor raises ApprovalRequired (in background)
        mock_executor.execute = AsyncMock(
            side_effect=ApprovalRequired(
                execution_id="pe-abc123",
                step_id="step1",
                token="approval-token-xyz",
                message="Manual approval required for deployment",
            )
        )

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call(
            "run_pipeline",
            {"name": "deploy", "inputs": {}, "session_id": "sess-1"},
        )

        assert result["success"] is True
        assert result["status"] == "running"
        assert result["execution_id"] == "pe-abc123"

    @pytest.mark.asyncio
    async def test_run_pipeline_not_found(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that run_pipeline returns error when pipeline not found."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        mock_loader.load_pipeline.return_value = None

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call(
            "run_pipeline",
            {"name": "nonexistent", "inputs": {}, "session_id": "sess-1"},
        )

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_run_pipeline_execution_error_returns_running(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that run_pipeline returns running even if executor will fail.

        Errors happen in background; callers discover them via get_pipeline_status.
        """
        from unittest.mock import AsyncMock

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        pipeline = PipelineDefinition(
            name="deploy",
            steps=[PipelineStep(id="step1", exec="echo deploy")],
        )
        mock_loader.load_pipeline.return_value = pipeline

        mock_executor.execute = AsyncMock(side_effect=RuntimeError("Execution failed"))

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call(
            "run_pipeline",
            {"name": "deploy", "inputs": {}, "session_id": "sess-1"},
        )

        # Fire-and-forget: returns success/running immediately
        assert result["success"] is True
        assert result["status"] == "running"
        assert result["execution_id"] == "pe-abc123"

    @pytest.mark.asyncio
    async def test_run_pipeline_no_executor_configured(
        self, mock_loader, mock_execution_manager
    ) -> None:
        """Test that run_pipeline returns error when no executor configured."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: None,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call(
            "run_pipeline",
            {"name": "deploy", "inputs": {}, "session_id": "sess-1"},
        )

        assert result["success"] is False
        assert "executor" in result["error"].lower()


class TestApprovePipelineTool:
    """Tests for the approve_pipeline MCP tool."""

    def test_registry_has_approve_pipeline_tool(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that approve_pipeline tool is registered."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        tools = registry.list_tools()
        tool_names = [t["name"] for t in tools]

        assert "approve_pipeline" in tool_names

    @pytest.mark.asyncio
    async def test_approve_pipeline_calls_executor_approve(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that approve_pipeline calls executor.approve()."""
        from unittest.mock import AsyncMock

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.COMPLETED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        mock_executor.approve = AsyncMock(return_value=execution)

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        await registry.call(
            "approve_pipeline",
            {"token": "approval-token-xyz", "approved_by": "user@example.com"},
        )

        mock_executor.approve.assert_called_once_with(
            token="approval-token-xyz",
            approved_by="user@example.com",
        )

    @pytest.mark.asyncio
    async def test_approve_pipeline_returns_execution_status(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that approve_pipeline returns execution status after approval."""
        from unittest.mock import AsyncMock

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.COMPLETED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            outputs_json='{"result": "deployed"}',
        )
        mock_executor.approve = AsyncMock(return_value=execution)

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call(
            "approve_pipeline",
            {"token": "approval-token-xyz"},
        )

        assert result["success"] is True
        assert result["status"] == "completed"
        assert result["execution_id"] == "pe-abc123"

    @pytest.mark.asyncio
    async def test_approve_pipeline_invalid_token(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that approve_pipeline returns error for invalid token."""
        from unittest.mock import AsyncMock

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        mock_executor.approve = AsyncMock(side_effect=ValueError("Invalid or expired token"))

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call(
            "approve_pipeline",
            {"token": "invalid-token"},
        )

        assert result["success"] is False
        assert "invalid" in result["error"].lower() or "token" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_approve_pipeline_no_executor(self, mock_loader, mock_execution_manager) -> None:
        """Test that approve_pipeline returns error when no executor configured."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: None,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call(
            "approve_pipeline",
            {"token": "approval-token-xyz"},
        )

        assert result["success"] is False
        assert "executor" in result["error"].lower()


class TestRejectPipelineTool:
    """Tests for the reject_pipeline MCP tool."""

    def test_registry_has_reject_pipeline_tool(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that reject_pipeline tool is registered."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        tools = registry.list_tools()
        tool_names = [t["name"] for t in tools]

        assert "reject_pipeline" in tool_names

    @pytest.mark.asyncio
    async def test_reject_pipeline_calls_executor_reject(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that reject_pipeline calls executor.reject()."""
        from unittest.mock import AsyncMock

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.CANCELLED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        mock_executor.reject = AsyncMock(return_value=execution)

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        await registry.call(
            "reject_pipeline",
            {"token": "approval-token-xyz", "rejected_by": "user@example.com"},
        )

        mock_executor.reject.assert_called_once_with(
            token="approval-token-xyz",
            rejected_by="user@example.com",
        )

    @pytest.mark.asyncio
    async def test_reject_pipeline_returns_cancelled_status(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that reject_pipeline returns cancelled status."""
        from unittest.mock import AsyncMock

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.CANCELLED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        mock_executor.reject = AsyncMock(return_value=execution)

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call(
            "reject_pipeline",
            {"token": "approval-token-xyz"},
        )

        assert result["success"] is True
        assert result["status"] == "cancelled"
        assert result["execution_id"] == "pe-abc123"

    @pytest.mark.asyncio
    async def test_reject_pipeline_invalid_token(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that reject_pipeline returns error for invalid token."""
        from unittest.mock import AsyncMock

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        mock_executor.reject = AsyncMock(side_effect=ValueError("Invalid or expired token"))

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call(
            "reject_pipeline",
            {"token": "invalid-token"},
        )

        assert result["success"] is False
        assert "invalid" in result["error"].lower() or "token" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_reject_pipeline_no_executor(self, mock_loader, mock_execution_manager) -> None:
        """Test that reject_pipeline returns error when no executor configured."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: None,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call(
            "reject_pipeline",
            {"token": "approval-token-xyz"},
        )

        assert result["success"] is False
        assert "executor" in result["error"].lower()


class TestGetPipelineStatusTool:
    """Tests for the get_pipeline_status MCP tool."""

    def test_registry_has_get_pipeline_status_tool(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that get_pipeline_status tool is registered."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        tools = registry.list_tools()
        tool_names = [t["name"] for t in tools]

        assert "get_pipeline_status" in tool_names

    @pytest.mark.asyncio
    async def test_get_pipeline_status_returns_execution_details(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that get_pipeline_status returns execution with all fields."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.RUNNING,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:01:00Z",
            inputs_json='{"env": "prod"}',
        )
        mock_execution_manager.get_execution.return_value = execution
        mock_execution_manager.get_steps_for_execution.return_value = []

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call(
            "get_pipeline_status",
            {"execution_id": "pe-abc123"},
        )

        assert result["success"] is True
        assert result["execution"]["id"] == "pe-abc123"
        assert result["execution"]["pipeline_name"] == "deploy"
        assert result["execution"]["status"] == "running"
        assert result["execution"]["inputs"] == {"env": "prod"}

    @pytest.mark.asyncio
    async def test_get_pipeline_status_includes_step_executions(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that get_pipeline_status includes step_executions list."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry
        from gobby.workflows.pipeline_state import (
            ExecutionStatus,
            PipelineExecution,
            StepExecution,
            StepStatus,
        )

        execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.RUNNING,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:01:00Z",
        )

        step1 = StepExecution(
            id=1,
            execution_id="pe-abc123",
            step_id="step1",
            status=StepStatus.COMPLETED,
            output_json='{"stdout": "done"}',
        )
        step2 = StepExecution(
            id=2,
            execution_id="pe-abc123",
            step_id="step2",
            status=StepStatus.RUNNING,
        )

        mock_execution_manager.get_execution.return_value = execution
        mock_execution_manager.get_steps_for_execution.return_value = [step1, step2]

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call(
            "get_pipeline_status",
            {"execution_id": "pe-abc123"},
        )

        assert result["success"] is True
        assert "steps" in result
        assert len(result["steps"]) == 2
        assert result["steps"][0]["step_id"] == "step1"
        assert result["steps"][0]["status"] == "completed"
        assert result["steps"][1]["step_id"] == "step2"
        assert result["steps"][1]["status"] == "running"

    @pytest.mark.asyncio
    async def test_get_pipeline_status_not_found(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that get_pipeline_status returns error when not found."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        mock_execution_manager.get_execution.return_value = None

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call(
            "get_pipeline_status",
            {"execution_id": "pe-nonexistent"},
        )

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_get_pipeline_status_no_execution_manager(
        self, mock_loader, mock_executor
    ) -> None:
        """Test that get_pipeline_status returns error when no manager configured."""
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: None,
        )

        result = await registry.call(
            "get_pipeline_status",
            {"execution_id": "pe-abc123"},
        )

        assert result["success"] is False
        assert "manager" in result["error"].lower() or "configured" in result["error"].lower()


class TestDynamicPipelineTools:
    """Tests for dynamic tool generation from pipelines with expose_as_tool=True."""

    @pytest.mark.asyncio
    async def test_expose_as_tool_creates_dynamic_tool(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that pipelines with expose_as_tool=True are exposed as tools."""
        from pathlib import Path

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        # Create a pipeline with expose_as_tool=True
        pipeline = PipelineDefinition(
            name="run-tests",
            description="Run the test suite",
            steps=[PipelineStep(id="test", exec="pytest")],
            expose_as_tool=True,
        )

        discovered = [
            DiscoveredWorkflow(
                name="run-tests",
                definition=pipeline,
                priority=100,
                is_project=True,
                path=Path("/project/.gobby/workflows/run-tests.yaml"),
            ),
        ]
        mock_loader.discover_pipeline_workflows.return_value = discovered
        mock_loader.discover_pipeline_workflows_sync.return_value = discovered

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        tools = registry.list_tools()
        tool_names = [t["name"] for t in tools]

        assert "pipeline:run-tests" in tool_names

    @pytest.mark.asyncio
    async def test_expose_as_tool_false_not_exposed(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that pipelines with expose_as_tool=False are NOT exposed as tools."""
        from pathlib import Path

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        # Create a pipeline without expose_as_tool (defaults to False)
        pipeline = PipelineDefinition(
            name="internal-pipeline",
            description="Internal pipeline not exposed",
            steps=[PipelineStep(id="step1", exec="echo internal")],
        )

        discovered = [
            DiscoveredWorkflow(
                name="internal-pipeline",
                definition=pipeline,
                priority=100,
                is_project=True,
                path=Path("/project/.gobby/workflows/internal.yaml"),
            ),
        ]
        mock_loader.discover_pipeline_workflows.return_value = discovered
        mock_loader.discover_pipeline_workflows_sync.return_value = discovered

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        tools = registry.list_tools()
        tool_names = [t["name"] for t in tools]

        assert "pipeline:internal-pipeline" not in tool_names

    @pytest.mark.asyncio
    async def test_dynamic_tool_has_correct_description(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that dynamic pipeline tools have the pipeline's description."""
        from pathlib import Path

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        pipeline = PipelineDefinition(
            name="deploy",
            description="Deploy to production environment",
            steps=[PipelineStep(id="deploy", exec="deploy-app")],
            expose_as_tool=True,
        )

        discovered = [
            DiscoveredWorkflow(
                name="deploy",
                definition=pipeline,
                priority=100,
                is_project=True,
                path=Path("/project/.gobby/workflows/deploy.yaml"),
            ),
        ]
        mock_loader.discover_pipeline_workflows.return_value = discovered
        mock_loader.discover_pipeline_workflows_sync.return_value = discovered

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        # Use get_schema to get the full schema with description
        schema = registry.get_schema("pipeline:deploy")
        assert schema is not None
        assert schema["description"] == "Deploy to production environment"

    @pytest.mark.asyncio
    async def test_dynamic_tool_includes_input_schema(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that dynamic pipeline tools include input schema from pipeline inputs."""
        from pathlib import Path

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        pipeline = PipelineDefinition(
            name="deploy",
            description="Deploy to environment",
            steps=[PipelineStep(id="deploy", exec="deploy-app")],
            expose_as_tool=True,
            inputs={
                "environment": {"type": "string", "default": "staging"},
                "version": {"type": "string", "description": "Version to deploy"},
            },
        )

        discovered = [
            DiscoveredWorkflow(
                name="deploy",
                definition=pipeline,
                priority=100,
                is_project=True,
                path=Path("/project/.gobby/workflows/deploy.yaml"),
            ),
        ]
        mock_loader.discover_pipeline_workflows.return_value = discovered
        mock_loader.discover_pipeline_workflows_sync.return_value = discovered

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        # Use get_schema to get the full schema with inputSchema
        schema = registry.get_schema("pipeline:deploy")
        assert schema is not None
        assert "inputSchema" in schema
        input_schema = schema["inputSchema"]
        assert "properties" in input_schema
        assert "environment" in input_schema["properties"]
        assert "version" in input_schema["properties"]

    @pytest.mark.asyncio
    async def test_dynamic_tool_executes_pipeline(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that calling a dynamic pipeline tool executes the pipeline."""
        from pathlib import Path
        from unittest.mock import AsyncMock

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        pipeline = PipelineDefinition(
            name="run-tests",
            description="Run tests",
            steps=[PipelineStep(id="test", exec="pytest")],
            expose_as_tool=True,
            inputs={"filter": {"type": "string", "default": ""}},
        )

        discovered = [
            DiscoveredWorkflow(
                name="run-tests",
                definition=pipeline,
                priority=100,
                is_project=True,
                path=Path("/project/.gobby/workflows/run-tests.yaml"),
            ),
        ]
        mock_loader.discover_pipeline_workflows.return_value = discovered
        mock_loader.discover_pipeline_workflows_sync.return_value = discovered
        mock_loader.load_pipeline.return_value = pipeline

        execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="run-tests",
            project_id="",
            status=ExecutionStatus.COMPLETED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            outputs_json='{"tests_passed": 42}',
        )
        mock_executor.execute = AsyncMock(return_value=execution)

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        result = await registry.call(
            "pipeline:run-tests",
            {"filter": "test_api", "session_id": "sess-1"},
        )

        assert result["success"] is True
        assert result["status"] == "running"
        assert result["execution_id"] == "pe-abc123"

    @pytest.mark.asyncio
    async def test_multiple_exposed_pipelines(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        """Test that multiple pipelines with expose_as_tool=True all get tools."""
        from pathlib import Path

        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        pipeline1 = PipelineDefinition(
            name="build",
            description="Build project",
            steps=[PipelineStep(id="build", exec="npm run build")],
            expose_as_tool=True,
        )
        pipeline2 = PipelineDefinition(
            name="test",
            description="Run tests",
            steps=[PipelineStep(id="test", exec="npm test")],
            expose_as_tool=True,
        )
        pipeline3 = PipelineDefinition(
            name="internal",
            description="Internal only",
            steps=[PipelineStep(id="internal", exec="echo internal")],
            expose_as_tool=False,
        )

        discovered = [
            DiscoveredWorkflow(
                name="build",
                definition=pipeline1,
                priority=100,
                is_project=True,
                path=Path("/project/.gobby/workflows/build.yaml"),
            ),
            DiscoveredWorkflow(
                name="test",
                definition=pipeline2,
                priority=100,
                is_project=True,
                path=Path("/project/.gobby/workflows/test.yaml"),
            ),
            DiscoveredWorkflow(
                name="internal",
                definition=pipeline3,
                priority=100,
                is_project=True,
                path=Path("/project/.gobby/workflows/internal.yaml"),
            ),
        ]
        mock_loader.discover_pipeline_workflows.return_value = discovered
        mock_loader.discover_pipeline_workflows_sync.return_value = discovered

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
        )

        tools = registry.list_tools()
        tool_names = [t["name"] for t in tools]

        assert "pipeline:build" in tool_names
        assert "pipeline:test" in tool_names
        assert "pipeline:internal" not in tool_names


class TestWaitForCompletionTool:
    """Tests for the wait_for_completion MCP tool."""

    @pytest.fixture
    def completion_registry(self) -> MagicMock:
        """Create a mock completion registry."""
        registry = MagicMock()
        registry.wait = AsyncMock()
        return registry

    @pytest.fixture
    def registry(self, mock_loader, mock_executor, mock_execution_manager, completion_registry):
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        return create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
            completion_registry=completion_registry,
        )

    @pytest.mark.asyncio
    async def test_wait_returns_result_on_success(self, registry, completion_registry) -> None:
        completion_registry.wait.return_value = {"status": "success", "run_id": "run-1"}

        result = await registry.call(
            "wait_for_completion", {"completion_id": "run-1", "timeout": 60}
        )

        assert result["success"] is True
        assert result["status"] == "success"
        assert result["completion_id"] == "run-1"
        completion_registry.wait.assert_awaited_once_with("run-1", timeout=60)

    @pytest.mark.asyncio
    async def test_wait_returns_error_when_not_registered(
        self, registry, completion_registry
    ) -> None:
        completion_registry.wait.side_effect = KeyError("not registered")

        result = await registry.call("wait_for_completion", {"completion_id": "bogus"})

        assert result["success"] is False
        assert "not registered" in result["error"]

    @pytest.mark.asyncio
    async def test_wait_returns_error_on_timeout(self, registry, completion_registry) -> None:
        completion_registry.wait.side_effect = TimeoutError()

        result = await registry.call(
            "wait_for_completion", {"completion_id": "run-1", "timeout": 5}
        )

        assert result["success"] is False
        assert "Timed out" in result["error"]
        assert result["timeout"] == 5

    @pytest.mark.asyncio
    async def test_wait_no_registry_returns_error(
        self, mock_loader, mock_executor, mock_execution_manager
    ) -> None:
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        registry = create_workflows_registry(
            loader=mock_loader,
            executor_getter=lambda: mock_executor,
            execution_manager_getter=lambda: mock_execution_manager,
            completion_registry=None,
        )

        result = await registry.call("wait_for_completion", {"completion_id": "run-1"})

        assert result["success"] is False
        assert "not available" in result["error"]

    @pytest.mark.asyncio
    async def test_wait_with_no_timeout(self, registry, completion_registry) -> None:
        completion_registry.wait.return_value = {"status": "success"}

        result = await registry.call("wait_for_completion", {"completion_id": "run-1"})

        assert result["success"] is True
        completion_registry.wait.assert_awaited_once_with("run-1", timeout=None)
