"""Tests for pipeline HTTP routes.

Tests FastAPI endpoints for pipeline execution, approval, and querying.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gobby.servers.routes.pipelines import _batch_load_cron_info, create_pipelines_router
from gobby.workflows.pipeline_state import ExecutionStatus, StepStatus

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_server() -> MagicMock:
    """Create a mock HTTPServer."""
    server = MagicMock()
    server.services = MagicMock()
    server.services.database = MagicMock()
    server.services.workflow_loader = MagicMock()
    server.services.get_pipeline_executor.return_value = MagicMock()
    return server


@pytest.fixture
def client(mock_server: MagicMock) -> TestClient:
    """Create a FastAPI TestClient with pipeline routes."""
    app = FastAPI()
    router = create_pipelines_router(mock_server)
    app.include_router(router)
    return TestClient(app)


# ═══════════════════════════════════════════════════════════════════════
# _batch_load_cron_info
# ═══════════════════════════════════════════════════════════════════════


class TestBatchLoadCronInfo:
    """Tests for _batch_load_cron_info helper."""

    def test_empty_execution_ids_returns_empty(self) -> None:
        result = _batch_load_cron_info(MagicMock(), [])
        assert result == {}

    def test_returns_cron_info_for_matched_executions(self) -> None:
        db = MagicMock()
        db.fetchall.return_value = [
            {
                "pipeline_execution_id": "pe-1",
                "cron_job_id": "cj-1",
                "name": "daily-backup",
                "cron_expr": "0 0 * * *",
            }
        ]
        result = _batch_load_cron_info(db, ["pe-1", "pe-2"])
        assert "pe-1" in result
        assert result["pe-1"]["name"] == "daily-backup"
        assert "pe-2" not in result

    def test_handles_database_error(self) -> None:
        db = MagicMock()
        db.fetchall.side_effect = RuntimeError("DB error")
        result = _batch_load_cron_info(db, ["pe-1"])
        assert result == {}


# ═══════════════════════════════════════════════════════════════════════
# GET /api/pipelines/executions
# ═══════════════════════════════════════════════════════════════════════


class TestListExecutions:
    """Tests for the list_executions endpoint."""

    def test_list_executions_returns_empty(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        with patch("gobby.storage.pipelines.LocalPipelineExecutionManager") as MockEM:
            em = MockEM.return_value
            em.list_executions.return_value = []
            em.count_executions.return_value = 0
            em.status_summary_for_executions.return_value = {}
            em.get_steps_for_executions.return_value = {}

            mock_server.services.database.fetchall.return_value = []

            response = client.get("/api/pipelines/executions")
            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 0
            assert data["limit"] == 50
            assert data["offset"] == 0
            assert data["status_summary"] == {}
            assert data["executions"] == []

    def test_list_executions_invalid_status(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        response = client.get("/api/pipelines/executions?status=invalid_status")
        assert response.status_code == 400

    @pytest.mark.parametrize(
        "query_string",
        [
            "limit=0",
            "limit=201",
            "offset=-1",
        ],
    )
    def test_list_executions_invalid_pagination_returns_422(
        self, client: TestClient, mock_server: MagicMock, query_string: str
    ) -> None:
        response = client.get(f"/api/pipelines/executions?{query_string}")
        assert response.status_code == 422

    def test_list_executions_with_results(self, client: TestClient, mock_server: MagicMock) -> None:
        with patch("gobby.storage.pipelines.LocalPipelineExecutionManager") as MockEM:
            mock_exec = MagicMock()
            mock_exec.id = "pe-1"
            mock_exec.pipeline_name = "test-pipe"
            mock_exec.project_id = "proj-1"
            mock_exec.status = ExecutionStatus.COMPLETED
            mock_exec.created_at = "2024-01-01"
            mock_exec.updated_at = "2024-01-01"
            mock_exec.completed_at = "2024-01-01"
            mock_exec.inputs_json = "{}"
            mock_exec.outputs_json = "{}"
            mock_exec.definition_json = "{}"
            mock_exec.parent_execution_id = None

            em = MockEM.return_value
            em.list_executions.return_value = [mock_exec]
            em.count_executions.return_value = 7
            em.status_summary_for_executions.return_value = {"completed": 7}
            em.get_steps_for_executions.return_value = {}

            mock_server.services.database.fetchall.return_value = []

            response = client.get("/api/pipelines/executions?limit=10&offset=0")
            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 7
            assert data["limit"] == 10
            assert data["offset"] == 0
            assert data["status_summary"] == {"completed": 7}
            assert data["executions"][0]["pipeline_name"] == "test-pipe"

    def test_list_executions_total_reflects_filter_scope(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        with patch("gobby.storage.pipelines.LocalPipelineExecutionManager") as MockEM:
            em = MockEM.return_value
            em.list_executions.return_value = []
            em.count_executions.return_value = 3
            em.status_summary_for_executions.return_value = {"running": 3}
            em.get_steps_for_executions.return_value = {}

            mock_server.services.database.fetchall.return_value = []

            response = client.get("/api/pipelines/executions?status=running")
            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 3
            # count_executions called with the status filter applied
            count_call = em.count_executions.call_args
            assert count_call.kwargs["status"] == ExecutionStatus.RUNNING


# ═══════════════════════════════════════════════════════════════════════
# GET /api/pipelines/executions/search
# ═══════════════════════════════════════════════════════════════════════


class TestSearchExecutions:
    """Tests for the search_executions endpoint."""

    def test_search_requires_query(self, client: TestClient, mock_server: MagicMock) -> None:
        response = client.get("/api/pipelines/executions/search?q=")
        assert response.status_code == 400

    def test_search_invalid_status(self, client: TestClient, mock_server: MagicMock) -> None:
        response = client.get("/api/pipelines/executions/search?q=test&status=bad")
        assert response.status_code == 400

    def test_search_returns_results(self, client: TestClient, mock_server: MagicMock) -> None:
        with patch("gobby.storage.pipelines.LocalPipelineExecutionManager") as MockEM:
            mock_exec = MagicMock()
            mock_exec.id = "pe-1"
            mock_exec.pipeline_name = "test-pipe"
            mock_exec.project_id = "proj-1"
            mock_exec.status = ExecutionStatus.COMPLETED
            mock_exec.created_at = "2024-01-01"
            mock_exec.updated_at = "2024-01-01"
            mock_exec.completed_at = "2024-01-01"

            em = MockEM.return_value
            em.search_executions.return_value = [mock_exec]
            em.count_search_executions.return_value = 4

            response = client.get("/api/pipelines/executions/search?q=test&limit=5&offset=0")
            assert response.status_code == 200
            data = response.json()
            assert data["total"] == 4
            assert data["limit"] == 5
            assert data["offset"] == 0
            assert data["query"] == "test"

    @pytest.mark.parametrize(
        "query_string",
        [
            "q=test&limit=0",
            "q=test&limit=201",
            "q=test&offset=-1",
        ],
    )
    def test_search_executions_invalid_pagination_returns_422(
        self, client: TestClient, mock_server: MagicMock, query_string: str
    ) -> None:
        response = client.get(f"/api/pipelines/executions/search?{query_string}")
        assert response.status_code == 422


# ═══════════════════════════════════════════════════════════════════════
# POST /api/pipelines/run
# ═══════════════════════════════════════════════════════════════════════


class TestRunPipeline:
    """Tests for the run_pipeline endpoint."""

    def test_run_requires_project_id(self, client: TestClient, mock_server: MagicMock) -> None:
        response = client.post(
            "/api/pipelines/run",
            json={"name": "test-pipeline", "project_id": ""},
        )
        assert response.status_code == 400

    def test_run_no_loader(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server.services.workflow_loader = None
        response = client.post(
            "/api/pipelines/run",
            json={"name": "test-pipeline", "project_id": "proj-1"},
        )
        assert response.status_code == 500

    def test_run_no_executor(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server.services.get_pipeline_executor.return_value = None
        response = client.post(
            "/api/pipelines/run",
            json={"name": "test-pipeline", "project_id": "proj-1"},
        )
        assert response.status_code == 500

    def test_run_pipeline_not_found(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_server.services.workflow_loader.load_pipeline = AsyncMock(return_value=None)
        response = client.post(
            "/api/pipelines/run",
            json={"name": "nonexistent", "project_id": "proj-1"},
        )
        assert response.status_code == 404

    def test_run_pipeline_success(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_pipeline = MagicMock()
        mock_server.services.workflow_loader.load_pipeline = AsyncMock(return_value=mock_pipeline)

        mock_execution = MagicMock()
        mock_execution.status = ExecutionStatus.COMPLETED
        mock_execution.id = "pe-123"
        mock_execution.pipeline_name = "test-pipeline"
        executor = mock_server.services.get_pipeline_executor.return_value
        executor.execute = AsyncMock(return_value=mock_execution)

        response = client.post(
            "/api/pipelines/run",
            json={"name": "test-pipeline", "project_id": "proj-1"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["execution_id"] == "pe-123"

    def test_run_pipeline_approval_required(
        self, client: TestClient, mock_server: MagicMock
    ) -> None:
        from gobby.workflows.pipeline_state import ApprovalRequired

        mock_pipeline = MagicMock()
        mock_server.services.workflow_loader.load_pipeline = AsyncMock(return_value=mock_pipeline)

        executor = mock_server.services.get_pipeline_executor.return_value
        executor.execute = AsyncMock(
            side_effect=ApprovalRequired(
                execution_id="pe-123",
                step_id="approval-step",
                token="tok-abc",
                message="Needs approval",
            )
        )

        response = client.post(
            "/api/pipelines/run",
            json={"name": "test-pipeline", "project_id": "proj-1"},
        )
        assert response.status_code == 202
        data = response.json()
        assert data["status"] == "waiting_approval"
        assert data["token"] == "tok-abc"

    def test_run_pipeline_execution_error(self, client: TestClient, mock_server: MagicMock) -> None:
        mock_pipeline = MagicMock()
        mock_server.services.workflow_loader.load_pipeline = AsyncMock(return_value=mock_pipeline)

        executor = mock_server.services.get_pipeline_executor.return_value
        executor.execute = AsyncMock(side_effect=RuntimeError("boom"))

        response = client.post(
            "/api/pipelines/run",
            json={"name": "test-pipeline", "project_id": "proj-1"},
        )
        assert response.status_code == 500


# ═══════════════════════════════════════════════════════════════════════
# GET /api/pipelines/{execution_id}
# ═══════════════════════════════════════════════════════════════════════


class TestGetExecution:
    """Tests for the get_execution endpoint."""

    def test_get_execution_not_found(self, client: TestClient, mock_server: MagicMock) -> None:
        with patch("gobby.storage.pipelines.LocalPipelineExecutionManager") as MockEM:
            em = MockEM.return_value
            em.get_execution.return_value = None

            response = client.get("/api/pipelines/pe-nonexistent")
            assert response.status_code == 404

    def test_get_execution_success(self, client: TestClient, mock_server: MagicMock) -> None:
        with patch("gobby.storage.pipelines.LocalPipelineExecutionManager") as MockEM:
            mock_exec = MagicMock()
            mock_exec.id = "pe-1"
            mock_exec.pipeline_name = "test-pipe"
            mock_exec.project_id = "proj-1"
            mock_exec.status = ExecutionStatus.RUNNING
            mock_exec.created_at = "2024-01-01"
            mock_exec.updated_at = "2024-01-01"
            mock_exec.completed_at = None
            mock_exec.inputs_json = "{}"
            mock_exec.outputs_json = None
            mock_exec.definition_json = "{}"
            mock_exec.parent_execution_id = None

            mock_step = MagicMock()
            mock_step.id = 1
            mock_step.step_id = "s1"
            mock_step.status = StepStatus.COMPLETED
            mock_step.started_at = "2024-01-01"
            mock_step.completed_at = "2024-01-01"
            mock_step.output_json = "{}"
            mock_step.error = None
            mock_step.approval_token = None

            em = MockEM.return_value
            em.get_execution.return_value = mock_exec
            em.get_steps_for_execution.return_value = [mock_step]

            mock_server.services.database.fetchall.return_value = []

            response = client.get("/api/pipelines/pe-1")
            assert response.status_code == 200
            data = response.json()
            assert data["id"] == "pe-1"
            assert len(data["steps"]) == 1


# ═══════════════════════════════════════════════════════════════════════
# POST /api/pipelines/approve/{token}
# ═══════════════════════════════════════════════════════════════════════


class TestApproveExecution:
    """Tests for the approve_execution endpoint."""

    def test_approve_invalid_token(self, client: TestClient, mock_server: MagicMock) -> None:
        with patch("gobby.storage.pipelines.LocalPipelineExecutionManager") as MockEM:
            em = MockEM.return_value
            em.get_step_by_approval_token.return_value = None

            response = client.post("/api/pipelines/approve/bad-token")
            assert response.status_code == 404

    def test_approve_execution_not_found(self, client: TestClient, mock_server: MagicMock) -> None:
        with patch("gobby.storage.pipelines.LocalPipelineExecutionManager") as MockEM:
            mock_step = MagicMock()
            mock_step.execution_id = "pe-gone"

            em = MockEM.return_value
            em.get_step_by_approval_token.return_value = mock_step
            em.get_execution.return_value = None

            response = client.post("/api/pipelines/approve/tok-1")
            assert response.status_code == 404

    def test_approve_no_executor(self, client: TestClient, mock_server: MagicMock) -> None:
        with patch("gobby.storage.pipelines.LocalPipelineExecutionManager") as MockEM:
            mock_step = MagicMock()
            mock_step.execution_id = "pe-1"
            mock_exec = MagicMock()
            mock_exec.project_id = "proj-1"

            em = MockEM.return_value
            em.get_step_by_approval_token.return_value = mock_step
            em.get_execution.return_value = mock_exec

            mock_server.services.get_pipeline_executor.return_value = None

            response = client.post("/api/pipelines/approve/tok-1")
            assert response.status_code == 500

    def test_approve_success(self, client: TestClient, mock_server: MagicMock) -> None:
        with patch("gobby.storage.pipelines.LocalPipelineExecutionManager") as MockEM:
            mock_step = MagicMock()
            mock_step.execution_id = "pe-1"
            mock_exec = MagicMock()
            mock_exec.project_id = "proj-1"
            mock_exec.id = "pe-1"
            mock_exec.pipeline_name = "test"
            mock_exec.status = ExecutionStatus.COMPLETED

            em = MockEM.return_value
            em.get_step_by_approval_token.return_value = mock_step
            em.get_execution.return_value = mock_exec

            executor = mock_server.services.get_pipeline_executor.return_value
            executor.approve = AsyncMock(return_value=mock_exec)

            response = client.post("/api/pipelines/approve/tok-1")
            assert response.status_code == 200

    def test_approve_raises_value_error(self, client: TestClient, mock_server: MagicMock) -> None:
        with patch("gobby.storage.pipelines.LocalPipelineExecutionManager") as MockEM:
            mock_step = MagicMock()
            mock_step.execution_id = "pe-1"
            mock_exec = MagicMock()
            mock_exec.project_id = "proj-1"

            em = MockEM.return_value
            em.get_step_by_approval_token.return_value = mock_step
            em.get_execution.return_value = mock_exec

            executor = mock_server.services.get_pipeline_executor.return_value
            executor.approve = AsyncMock(side_effect=ValueError("Bad token"))

            response = client.post("/api/pipelines/approve/tok-bad")
            assert response.status_code == 404


# ═══════════════════════════════════════════════════════════════════════
# POST /api/pipelines/reject/{token}
# ═══════════════════════════════════════════════════════════════════════


class TestRejectExecution:
    """Tests for the reject_execution endpoint."""

    def test_reject_invalid_token(self, client: TestClient, mock_server: MagicMock) -> None:
        with patch("gobby.storage.pipelines.LocalPipelineExecutionManager") as MockEM:
            em = MockEM.return_value
            em.get_step_by_approval_token.return_value = None

            response = client.post("/api/pipelines/reject/bad-token")
            assert response.status_code == 404

    def test_reject_success(self, client: TestClient, mock_server: MagicMock) -> None:
        with patch("gobby.storage.pipelines.LocalPipelineExecutionManager") as MockEM:
            mock_step = MagicMock()
            mock_step.execution_id = "pe-1"
            mock_exec = MagicMock()
            mock_exec.project_id = "proj-1"
            mock_exec.id = "pe-1"
            mock_exec.pipeline_name = "test"
            mock_exec.status = ExecutionStatus.CANCELLED

            em = MockEM.return_value
            em.get_step_by_approval_token.return_value = mock_step
            em.get_execution.return_value = mock_exec

            executor = mock_server.services.get_pipeline_executor.return_value
            executor.reject = AsyncMock(return_value=mock_exec)

            response = client.post("/api/pipelines/reject/tok-1")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "cancelled"

    def test_reject_raises_value_error(self, client: TestClient, mock_server: MagicMock) -> None:
        with patch("gobby.storage.pipelines.LocalPipelineExecutionManager") as MockEM:
            mock_step = MagicMock()
            mock_step.execution_id = "pe-1"
            mock_exec = MagicMock()
            mock_exec.project_id = "proj-1"

            em = MockEM.return_value
            em.get_step_by_approval_token.return_value = mock_step
            em.get_execution.return_value = mock_exec

            executor = mock_server.services.get_pipeline_executor.return_value
            executor.reject = AsyncMock(side_effect=ValueError("Bad token"))

            response = client.post("/api/pipelines/reject/tok-bad")
            assert response.status_code == 404
