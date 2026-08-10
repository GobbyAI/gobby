"""Tests for HTTP pipeline endpoints."""

from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from gobby.app_context import ServiceContainer
from gobby.config.bootstrap import BootstrapConfig
from gobby.servers.http import HTTPServer
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)
_UPDATED_AT = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)


@pytest.fixture
def session_storage(temp_db: HubDatabase) -> SessionManager:
    """Create session storage."""
    return SessionManager(temp_db)


@pytest.fixture
def mock_pipeline_executor() -> MagicMock:
    """Create a mock pipeline executor."""
    return MagicMock()


@pytest.fixture
def mock_workflow_loader() -> AsyncMock:
    """Create a mock workflow loader."""
    return AsyncMock()


@pytest.fixture
def http_server(
    session_storage: SessionManager,
    mock_pipeline_executor: MagicMock,
    mock_workflow_loader: AsyncMock,
) -> HTTPServer:
    """Create an HTTP server instance for testing."""
    services = ServiceContainer(
        config=None,
        database=session_storage.db,
        session_manager=session_storage,
        task_manager=MagicMock(),
        pipeline_executor=mock_pipeline_executor,
        workflow_loader=mock_workflow_loader,
    )
    # Route handler calls get_pipeline_executor(project_id) instead of accessing
    # pipeline_executor directly
    services.__dict__["get_pipeline_executor"] = MagicMock(return_value=mock_pipeline_executor)
    return HTTPServer(
        services=services,
        port=60887,
        test_mode=True,
        bootstrap_config=BootstrapConfig(auth_mode="disabled"),
    )


@pytest.fixture
def client(http_server: HTTPServer) -> Iterator[TestClient]:
    """Create a test client for the HTTP server."""
    with patch("gobby.servers.app_factory.HookManager") as MockHM:
        mock_instance = MockHM.return_value
        mock_instance._stop_registry = MagicMock()
        mock_instance.shutdown = MagicMock()
        mock_instance.shutdown_async = AsyncMock()
        with TestClient(http_server.app) as client:
            yield client


def _pipeline_executor(server: HTTPServer) -> MagicMock:
    executor = server.services.pipeline_executor
    assert isinstance(executor, MagicMock)
    return executor


def _workflow_loader(server: HTTPServer) -> AsyncMock:
    loader = server.services.workflow_loader
    assert isinstance(loader, AsyncMock)
    return loader


class TestPipelinesRunEndpoint:
    """Tests for POST /api/pipelines/run endpoint."""

    def test_run_pipeline_success(self, client: TestClient, http_server: HTTPServer) -> None:
        """Verify POST /api/pipelines/run returns 200 with execution details."""
        from gobby.workflows.definitions import PipelineDefinition, PipelineStep
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        # Setup mock loader
        mock_pipeline = PipelineDefinition(
            name="deploy",
            description="Deploy to production",
            steps=[PipelineStep(id="build", exec="npm run build")],
        )
        _workflow_loader(http_server).load_pipeline.return_value = mock_pipeline

        # Setup mock executor
        mock_execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.COMPLETED,
            created_at=_CREATED_AT,
            updated_at=_UPDATED_AT,
        )
        _pipeline_executor(http_server).execute = AsyncMock(return_value=mock_execution)

        response = client.post(
            "/api/pipelines/run",
            json={"name": "deploy", "inputs": {"env": "prod"}, "project_id": "proj-1"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["execution_id"] == "pe-abc123"

    def test_run_pipeline_records_execution_for_requested_project(
        self, temp_db: HubDatabase
    ) -> None:
        """A project B request must not use project A's startup execution manager."""
        from gobby.storage.pipelines import LocalPipelineExecutionManager
        from gobby.workflows.definitions import PipelineDefinition, PipelineStep
        from gobby.workflows.pipeline_executor import PipelineExecutor

        project_a = "00000000-0000-0000-0000-00000000000a"
        project_b = "00000000-0000-0000-0000-00000000000b"
        for project_id, name in ((project_a, "Project A"), (project_b, "Project B")):
            temp_db.execute(
                """
                INSERT INTO projects (id, name, created_at, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (project_id, name),
            )

        loader = AsyncMock()
        loader.load_pipeline.return_value = PipelineDefinition(
            name="cross-project",
            steps=[
                PipelineStep(
                    id="skip",
                    exec="must-not-run",
                    condition="${{ inputs.get('run') }}",
                )
            ],
        )
        startup_manager = LocalPipelineExecutionManager(temp_db, project_id=project_a)
        startup_executor = PipelineExecutor(
            db=temp_db,
            execution_manager=startup_manager,
            llm_service=None,
            loader=loader,
        )
        services = ServiceContainer(
            config=None,
            database=temp_db,
            session_manager=None,
            task_manager=MagicMock(),
            pipeline_executor=startup_executor,
            workflow_loader=loader,
            pipeline_execution_manager=startup_manager,
            project_id=project_a,
        )
        server = HTTPServer(
            services=services,
            port=60887,
            test_mode=True,
            bootstrap_config=BootstrapConfig(auth_mode="disabled"),
        )

        with TestClient(server.app) as client:
            response = client.post(
                "/api/pipelines/run",
                json={"name": "cross-project", "inputs": {}, "project_id": project_b},
            )

        assert response.status_code == 200
        execution_id = response.json()["execution_id"]
        row = temp_db.fetchone(
            "SELECT project_id FROM pipeline_executions WHERE id = %s",
            (execution_id,),
        )
        assert row is not None
        assert str(row["project_id"]) == project_b

    def test_run_pipeline_not_found(self, client: TestClient, http_server: HTTPServer) -> None:
        """Verify POST /api/pipelines/run returns 404 for unknown pipeline."""
        _workflow_loader(http_server).load_pipeline.return_value = None

        response = client.post(
            "/api/pipelines/run",
            json={"name": "nonexistent", "inputs": {}, "project_id": "proj-1"},
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_run_pipeline_approval_required(
        self, client: TestClient, http_server: HTTPServer
    ) -> None:
        """Verify POST /api/pipelines/run returns 202 when approval is needed."""
        from gobby.workflows.definitions import PipelineDefinition, PipelineStep
        from gobby.workflows.pipeline_state import ApprovalRequired

        # Setup mock loader
        mock_pipeline = PipelineDefinition(
            name="deploy",
            description="Deploy to production",
            steps=[PipelineStep(id="build", exec="npm run build")],
        )
        _workflow_loader(http_server).load_pipeline.return_value = mock_pipeline

        # Setup mock executor to raise ApprovalRequired
        _pipeline_executor(http_server).execute = AsyncMock(
            side_effect=ApprovalRequired(
                execution_id="pe-abc123",
                step_id="deploy-step",
                token="approval-token-xyz",
                message="Manual approval required",
            )
        )

        response = client.post(
            "/api/pipelines/run",
            json={"name": "deploy", "inputs": {}, "project_id": "proj-1"},
        )

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "waiting_approval"
        assert data["token"] == "approval-token-xyz"
        assert data["execution_id"] == "pe-abc123"

    def test_run_pipeline_execution_error(
        self, client: TestClient, http_server: HTTPServer
    ) -> None:
        """Verify POST /api/pipelines/run returns 500 on execution error."""
        from gobby.workflows.definitions import PipelineDefinition, PipelineStep

        # Setup mock loader
        mock_pipeline = PipelineDefinition(
            name="deploy",
            description="Deploy to production",
            steps=[PipelineStep(id="build", exec="npm run build")],
        )
        _workflow_loader(http_server).load_pipeline.return_value = mock_pipeline

        # Setup mock executor to raise an error
        _pipeline_executor(http_server).execute = AsyncMock(
            side_effect=RuntimeError("Execution failed")
        )

        response = client.post(
            "/api/pipelines/run",
            json={"name": "deploy", "inputs": {}, "project_id": "proj-1"},
        )

        assert response.status_code == 500
        assert "error" in response.json()["detail"].lower()


class TestPipelinesGetEndpoint:
    """Tests for GET /api/pipelines/{execution_id} endpoint."""

    @pytest.fixture
    def mock_execution_manager(self) -> MagicMock:
        """Create a mock execution manager."""
        return MagicMock()

    def test_get_execution_success(
        self, client: TestClient, mock_execution_manager: MagicMock
    ) -> None:
        """Verify GET /api/pipelines/{id} returns execution details."""
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        mock_execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.COMPLETED,
            created_at=_CREATED_AT,
            updated_at=_UPDATED_AT,
        )
        mock_execution_manager.get_execution.return_value = mock_execution
        mock_execution_manager.get_steps_for_execution.return_value = []

        with patch(
            "gobby.storage.pipelines.LocalPipelineExecutionManager",
            return_value=mock_execution_manager,
        ):
            response = client.get("/api/pipelines/pe-abc123")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "pe-abc123"
        assert data["pipeline_name"] == "deploy"
        assert data["status"] == "completed"

    def test_get_execution_includes_steps(
        self, client: TestClient, mock_execution_manager: MagicMock
    ) -> None:
        """Verify GET /api/pipelines/{id} includes step_executions array."""
        from gobby.workflows.pipeline_state import (
            ExecutionStatus,
            PipelineExecution,
            StepExecution,
            StepStatus,
        )

        mock_execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.RUNNING,
            created_at=_CREATED_AT,
            updated_at=_UPDATED_AT,
        )
        mock_steps = [
            StepExecution(
                id=1,
                execution_id="pe-abc123",
                step_id="build",
                status=StepStatus.COMPLETED,
            ),
            StepExecution(
                id=2,
                execution_id="pe-abc123",
                step_id="test",
                status=StepStatus.RUNNING,
            ),
        ]
        mock_execution_manager.get_execution.return_value = mock_execution
        mock_execution_manager.get_steps_for_execution.return_value = mock_steps

        with patch(
            "gobby.storage.pipelines.LocalPipelineExecutionManager",
            return_value=mock_execution_manager,
        ):
            response = client.get("/api/pipelines/pe-abc123")

        assert response.status_code == 200
        data = response.json()
        assert "steps" in data
        assert len(data["steps"]) == 2
        assert data["steps"][0]["step_id"] == "build"
        assert data["steps"][0]["status"] == "completed"
        assert data["steps"][1]["step_id"] == "test"
        assert data["steps"][1]["status"] == "running"

    def test_get_execution_not_found(
        self, client: TestClient, mock_execution_manager: MagicMock
    ) -> None:
        """Verify GET /api/pipelines/{id} returns 404 for unknown id."""
        mock_execution_manager.get_execution.return_value = None

        with patch(
            "gobby.storage.pipelines.LocalPipelineExecutionManager",
            return_value=mock_execution_manager,
        ):
            response = client.get("/api/pipelines/pe-nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


class TestPipelinesApproveEndpoint:
    """Tests for POST /api/pipelines/approve/{token} endpoint."""

    @pytest.fixture
    def mock_execution_manager(self) -> MagicMock:
        """Create a mock execution manager for approve lookups."""
        mgr = MagicMock()
        # Default: step found, execution found
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution, StepStatus

        mock_step = MagicMock(
            execution_id="pe-abc123",
            status=StepStatus.WAITING_APPROVAL,
        )
        mgr.get_step_by_approval_token.return_value = mock_step

        mgr.get_execution.return_value = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.RUNNING,
            created_at=_CREATED_AT,
            updated_at=_UPDATED_AT,
        )
        return mgr

    def test_approve_success(
        self, client: TestClient, http_server: HTTPServer, mock_execution_manager: MagicMock
    ) -> None:
        """Verify POST /api/pipelines/approve/{token} calls executor.approve()."""
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        mock_execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.COMPLETED,
            created_at=_CREATED_AT,
            updated_at=_UPDATED_AT,
        )
        _pipeline_executor(http_server).approve = AsyncMock(return_value=mock_execution)

        with patch(
            "gobby.storage.pipelines.LocalPipelineExecutionManager",
            return_value=mock_execution_manager,
        ):
            response = client.post("/api/pipelines/approve/approval-token-xyz")

        assert response.status_code == 200
        _pipeline_executor(http_server).approve.assert_called_once_with(
            "approval-token-xyz", approved_by=None
        )
        data = response.json()
        assert data["status"] == "completed"
        assert data["execution_id"] == "pe-abc123"

    def test_approve_invalid_token(
        self, client: TestClient, mock_execution_manager: MagicMock
    ) -> None:
        """Verify POST /api/pipelines/approve/{token} returns 404 for invalid token."""
        mock_execution_manager.get_step_by_approval_token.return_value = None

        with patch(
            "gobby.storage.pipelines.LocalPipelineExecutionManager",
            return_value=mock_execution_manager,
        ):
            response = client.post("/api/pipelines/approve/invalid-token")

        assert response.status_code == 404
        assert "invalid" in response.json()["detail"].lower()

    def test_approve_returns_next_approval(
        self, client: TestClient, http_server: HTTPServer, mock_execution_manager: MagicMock
    ) -> None:
        """Verify POST /api/pipelines/approve returns 202 if more approvals needed."""
        from gobby.workflows.pipeline_state import ApprovalRequired

        _pipeline_executor(http_server).approve = AsyncMock(
            side_effect=ApprovalRequired(
                execution_id="pe-abc123",
                step_id="deploy-step",
                token="next-approval-token",
                message="Another approval required",
            )
        )

        with patch(
            "gobby.storage.pipelines.LocalPipelineExecutionManager",
            return_value=mock_execution_manager,
        ):
            response = client.post("/api/pipelines/approve/approval-token-xyz")

        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "waiting_approval"
        assert data["token"] == "next-approval-token"


class TestPipelinesRejectEndpoint:
    """Tests for POST /api/pipelines/reject/{token} endpoint."""

    @pytest.fixture
    def mock_execution_manager(self) -> MagicMock:
        """Create a mock execution manager for reject lookups."""
        mgr = MagicMock()
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution, StepStatus

        mock_step = MagicMock(
            execution_id="pe-abc123",
            status=StepStatus.WAITING_APPROVAL,
        )
        mgr.get_step_by_approval_token.return_value = mock_step

        mgr.get_execution.return_value = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.RUNNING,
            created_at=_CREATED_AT,
            updated_at=_UPDATED_AT,
        )
        return mgr

    def test_reject_success(
        self, client: TestClient, http_server: HTTPServer, mock_execution_manager: MagicMock
    ) -> None:
        """Verify POST /api/pipelines/reject/{token} calls executor.reject()."""
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        mock_execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.FAILED,
            created_at=_CREATED_AT,
            updated_at=_UPDATED_AT,
        )
        _pipeline_executor(http_server).reject = AsyncMock(return_value=mock_execution)

        with patch(
            "gobby.storage.pipelines.LocalPipelineExecutionManager",
            return_value=mock_execution_manager,
        ):
            response = client.post("/api/pipelines/reject/approval-token-xyz")

        assert response.status_code == 200
        _pipeline_executor(http_server).reject.assert_called_once_with(
            "approval-token-xyz", rejected_by=None
        )
        data = response.json()
        assert data["status"] == "failed"
        assert data["execution_id"] == "pe-abc123"

    def test_reject_invalid_token(
        self, client: TestClient, mock_execution_manager: MagicMock
    ) -> None:
        """Verify POST /api/pipelines/reject/{token} returns 404 for invalid token."""
        mock_execution_manager.get_step_by_approval_token.return_value = None

        with patch(
            "gobby.storage.pipelines.LocalPipelineExecutionManager",
            return_value=mock_execution_manager,
        ):
            response = client.post("/api/pipelines/reject/invalid-token")

        assert response.status_code == 404
        assert "invalid" in response.json()["detail"].lower()
