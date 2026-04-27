"""Tests for pipeline execution query tools (list and search)."""

from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.workflows._pipeline_query import (
    list_pipeline_executions,
    search_pipeline_executions,
)
from gobby.workflows.pipeline_state import (
    ExecutionStatus,
    PipelineExecution,
    StepExecution,
    StepStatus,
)

pytestmark = pytest.mark.unit


def _make_execution(
    id: str = "pe-abc123",
    pipeline_name: str = "deploy",
    status: ExecutionStatus = ExecutionStatus.COMPLETED,
    **kwargs,
) -> PipelineExecution:
    defaults = {
        "project_id": "proj-1",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:01:00Z",
    }
    defaults.update(kwargs)
    return PipelineExecution(id=id, pipeline_name=pipeline_name, status=status, **defaults)


def _make_step(
    step_id: str = "build",
    status: StepStatus = StepStatus.COMPLETED,
    **kwargs,
) -> StepExecution:
    defaults = {
        "id": 1,
        "execution_id": "pe-abc123",
    }
    defaults.update(kwargs)
    return StepExecution(step_id=step_id, status=status, **defaults)


@pytest.fixture
def mock_em() -> MagicMock:
    """Mock execution manager with defaults."""
    em = MagicMock()
    em.list_executions.return_value = []
    em.search_executions.return_value = []
    em.count_executions.return_value = 0
    em.count_search_executions.return_value = 0
    em.status_summary_for_executions.return_value = {}
    em.count_by_status.return_value = {}
    em.get_steps_for_executions.return_value = {}
    return em


class TestListPipelineExecutions:
    """Tests for list_pipeline_executions."""

    def test_basic_list(self, mock_em) -> None:
        execs = [_make_execution(), _make_execution(id="pe-def456", status=ExecutionStatus.FAILED)]
        mock_em.list_executions.return_value = execs
        mock_em.count_executions.return_value = 2
        mock_em.status_summary_for_executions.return_value = {"completed": 1, "failed": 1}

        result = list_pipeline_executions(mock_em)

        assert result["success"] is True
        assert result["total"] == 2
        assert result["limit"] == 50
        assert result["offset"] == 0
        assert len(result["executions"]) == 2
        assert result["status_summary"] == {"completed": 1, "failed": 1}

    def test_brief_mode(self, mock_em) -> None:
        mock_em.list_executions.return_value = [_make_execution()]

        result = list_pipeline_executions(mock_em, brief=True)

        entry = result["executions"][0]
        assert "id" in entry
        assert "pipeline_name" in entry
        assert "status" in entry
        assert "created_at" in entry
        # Brief mode should NOT include session_id, project_id, etc.
        assert "session_id" not in entry
        assert "project_id" not in entry

    def test_full_mode(self, mock_em) -> None:
        mock_em.list_executions.return_value = [_make_execution()]

        result = list_pipeline_executions(mock_em, brief=False)

        entry = result["executions"][0]
        assert "project_id" in entry
        assert "session_id" in entry

    def test_include_steps(self, mock_em) -> None:
        ex = _make_execution()
        mock_em.list_executions.return_value = [ex]
        mock_em.get_steps_for_executions.return_value = {
            ex.id: [_make_step(step_id="build"), _make_step(step_id="test")]
        }

        result = list_pipeline_executions(mock_em, include_steps=True)

        assert "steps" in result["executions"][0]
        assert len(result["executions"][0]["steps"]) == 2

    def test_invalid_status_error(self, mock_em) -> None:
        result = list_pipeline_executions(mock_em, status="bogus")

        assert result["success"] is False
        assert "Invalid status" in result["error"]

    def test_filters_passed_through(self, mock_em) -> None:
        list_pipeline_executions(
            mock_em,
            status="running",
            pipeline_name="deploy",
            session_id="sess-1",
            parent_execution_id="pe-parent",
            limit=10,
            offset=5,
        )

        mock_em.list_executions.assert_called_once_with(
            status=ExecutionStatus.RUNNING,
            pipeline_name="deploy",
            session_id="sess-1",
            parent_execution_id="pe-parent",
            limit=10,
            offset=5,
        )
        mock_em.count_executions.assert_called_once_with(
            status=ExecutionStatus.RUNNING,
            pipeline_name="deploy",
            session_id="sess-1",
            parent_execution_id="pe-parent",
        )
        mock_em.status_summary_for_executions.assert_called_once_with(
            pipeline_name="deploy",
            session_id="sess-1",
            parent_execution_id="pe-parent",
        )

    def test_empty_results(self, mock_em) -> None:
        result = list_pipeline_executions(mock_em)

        assert result["success"] is True
        assert result["total"] == 0
        assert result["limit"] == 50
        assert result["offset"] == 0
        assert result["executions"] == []

    def test_invalid_pagination_returns_error_envelope(self, mock_em) -> None:
        mock_em.list_executions.side_effect = ValueError("limit must be > 0, got 0")

        result = list_pipeline_executions(mock_em, limit=0)

        assert result["success"] is False
        assert "Invalid pagination" in result["error"]

    def test_offset_progression_distinct_calls(self, mock_em) -> None:
        list_pipeline_executions(mock_em, limit=10, offset=0)
        list_pipeline_executions(mock_em, limit=10, offset=10)

        calls = mock_em.list_executions.call_args_list
        assert len(calls) == 2
        assert calls[0].kwargs["offset"] == 0
        assert calls[1].kwargs["offset"] == 10


class TestSearchPipelineExecutions:
    """Tests for search_pipeline_executions."""

    def test_basic_search(self, mock_em) -> None:
        mock_em.search_executions.return_value = [_make_execution()]
        mock_em.count_search_executions.return_value = 1

        result = search_pipeline_executions(mock_em, query="deploy")

        assert result["success"] is True
        assert result["total"] == 1
        assert result["limit"] == 20
        assert result["offset"] == 0
        assert result["query"] == "deploy"

    def test_empty_query_error(self, mock_em) -> None:
        result = search_pipeline_executions(mock_em, query="")
        assert result["success"] is False
        assert "empty" in result["error"].lower()

    def test_whitespace_query_error(self, mock_em) -> None:
        result = search_pipeline_executions(mock_em, query="   ")
        assert result["success"] is False

    def test_invalid_status_error(self, mock_em) -> None:
        result = search_pipeline_executions(mock_em, query="deploy", status="bogus")
        assert result["success"] is False
        assert "Invalid status" in result["error"]

    def test_search_params_passed_through(self, mock_em) -> None:
        search_pipeline_executions(
            mock_em,
            query="deploy",
            search_errors=False,
            search_outputs=True,
            status="failed",
            limit=5,
            offset=2,
        )

        mock_em.search_executions.assert_called_once_with(
            query="deploy",
            search_errors=False,
            search_outputs=True,
            status=ExecutionStatus.FAILED,
            limit=5,
            offset=2,
        )
        mock_em.count_search_executions.assert_called_once_with(
            query="deploy",
            search_errors=False,
            search_outputs=True,
            status=ExecutionStatus.FAILED,
        )

    def test_search_invalid_pagination_returns_error_envelope(self, mock_em) -> None:
        mock_em.search_executions.side_effect = ValueError("offset must be >= 0, got -1")

        result = search_pipeline_executions(mock_em, query="deploy", offset=-1)

        assert result["success"] is False
        assert "Invalid pagination" in result["error"]

    def test_include_steps(self, mock_em) -> None:
        ex = _make_execution()
        mock_em.search_executions.return_value = [ex]
        mock_em.get_steps_for_executions.return_value = {
            ex.id: [_make_step(step_id="build", error="Connection refused")]
        }

        result = search_pipeline_executions(mock_em, query="deploy", include_steps=True)

        assert "steps" in result["executions"][0]
        assert result["executions"][0]["steps"][0]["error"] == "Connection refused"
