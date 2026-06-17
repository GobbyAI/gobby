"""Tests for cron MCP proxy tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.cron import create_cron_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.scheduler.scheduler import CronRunRejected
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob, CronRun, CronRunChild

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000000"


def _make_job(**overrides: object) -> CronJob:
    defaults = {
        "id": "cj-abc123",
        "project_id": PROJECT_ID,
        "name": "Test Job",
        "schedule_type": "cron",
        "cron_expr": "0 7 * * *",
        "interval_seconds": None,
        "run_at": None,
        "timezone": "UTC",
        "action_type": "shell",
        "action_config": {"command": "echo"},
        "enabled": True,
        "next_run_at": None,
        "last_run_at": None,
        "last_status": None,
        "consecutive_failures": 0,
        "description": None,
        "created_at": "2026-02-10T00:00:00+00:00",
        "updated_at": "2026-02-10T00:00:00+00:00",
    }
    defaults.update(overrides)
    return CronJob(**defaults)


def _make_run(**overrides: object) -> CronRun:
    defaults = {
        "id": "cr-run123",
        "cron_job_id": "cj-abc123",
        "triggered_at": "2026-02-10T07:00:00+00:00",
        "started_at": None,
        "completed_at": None,
        "status": "pending",
        "output": None,
        "error": None,
        "agent_run_id": None,
        "pipeline_execution_id": None,
        "created_at": "2026-02-10T07:00:00+00:00",
    }
    defaults.update(overrides)
    return CronRun(**defaults)


@pytest.fixture
def mock_storage() -> MagicMock:
    return MagicMock(spec=CronJobStorage)


@pytest.fixture
def mock_scheduler() -> MagicMock:
    mock = MagicMock()
    mock.run_now = AsyncMock()
    return mock


@pytest.fixture
def registry(mock_storage: MagicMock, mock_scheduler: MagicMock) -> InternalToolRegistry:
    return create_cron_registry(cron_storage=mock_storage, cron_scheduler=mock_scheduler)


class TestListCronJobs:
    def test_list_returns_jobs(self, registry, mock_storage) -> None:
        mock_storage.list_jobs.return_value = [_make_job(), _make_job(id="cj-def")]
        tool = registry.get_tool("list_cron_jobs")
        result = tool()
        assert result["success"] is True
        assert result["count"] == 2

    def test_list_with_filters(self, registry, mock_storage) -> None:
        mock_storage.list_jobs.return_value = []
        tool = registry.get_tool("list_cron_jobs")
        result = tool(project_id=PROJECT_ID, enabled=True)
        assert result["success"] is True
        mock_storage.list_jobs.assert_called_once_with(
            project_id=PROJECT_ID,
            enabled=True,
            exclude_removed_automation=True,
        )

    def test_list_filters_removed_automation_rows(self, registry, mock_storage) -> None:
        mock_storage.list_jobs.return_value = [_make_job(name="User Job")]
        tool = registry.get_tool("list_cron_jobs")
        result = tool()
        assert result["success"] is True
        assert result["count"] == 1
        assert result["jobs"][0]["name"] == "User Job"
        mock_storage.list_jobs.assert_called_once_with(
            project_id=None,
            enabled=None,
            exclude_removed_automation=True,
        )


def test_internal_writers_not_exposed_via_mcp(registry: InternalToolRegistry) -> None:
    tool_names = {tool["name"] for tool in registry.list_tools()}

    assert "update_system_job_bookkeeping" not in tool_names
    assert "reconcile_system_job_definition" not in tool_names


class TestCreateCronJob:
    def test_create_success(self, registry, mock_storage) -> None:
        mock_storage.create_job.return_value = _make_job()
        tool = registry.get_tool("create_cron_job")
        result = tool(
            name="Test",
            action_type="shell",
            action_config={"command": "echo"},
            cron_expr="0 7 * * *",
        )
        assert result["success"] is True
        assert result["job"]["name"] == "Test Job"


class TestGetCronJob:
    def test_get_found(self, registry, mock_storage) -> None:
        mock_storage.get_job.return_value = _make_job()
        tool = registry.get_tool("get_cron_job")
        result = tool(job_id="cj-abc123")
        assert result["success"] is True
        assert result["job"]["id"] == "cj-abc123"

    def test_get_not_found(self, registry, mock_storage) -> None:
        mock_storage.get_job.return_value = None
        tool = registry.get_tool("get_cron_job")
        result = tool(job_id="cj-nonexistent")
        assert result["success"] is False


class TestUpdateCronJob:
    def test_update_success(self, registry, mock_storage) -> None:
        mock_storage.update_job.return_value = _make_job(name="Updated")
        tool = registry.get_tool("update_cron_job")
        result = tool(job_id="cj-abc123", name="Updated")
        assert result["success"] is True
        assert result["job"]["name"] == "Updated"

    def test_update_no_fields(self, registry, mock_storage) -> None:
        tool = registry.get_tool("update_cron_job")
        result = tool(job_id="cj-abc123")
        assert result["success"] is False
        assert "No fields" in result["error"]

    def test_update_not_found(self, registry, mock_storage) -> None:
        mock_storage.update_job.return_value = None
        tool = registry.get_tool("update_cron_job")
        result = tool(job_id="cj-nonexistent", name="X")
        assert result["success"] is False


class TestToggleCronJob:
    def test_toggle_success(self, registry, mock_storage) -> None:
        mock_storage.toggle_job.return_value = _make_job(enabled=False)
        tool = registry.get_tool("toggle_cron_job")
        result = tool(job_id="cj-abc123")
        assert result["success"] is True
        assert result["state"] == "disabled"

    def test_toggle_not_found(self, registry, mock_storage) -> None:
        mock_storage.toggle_job.return_value = None
        tool = registry.get_tool("toggle_cron_job")
        result = tool(job_id="cj-nonexistent")
        assert result["success"] is False


class TestDeleteCronJob:
    def test_delete_success(self, registry, mock_storage) -> None:
        mock_storage.delete_job.return_value = True
        tool = registry.get_tool("delete_cron_job")
        result = tool(job_id="cj-abc123")
        assert result["success"] is True

    def test_delete_not_found(self, registry, mock_storage) -> None:
        mock_storage.delete_job.return_value = False
        tool = registry.get_tool("delete_cron_job")
        result = tool(job_id="cj-nonexistent")
        assert result["success"] is False


class TestListCronRuns:
    def test_list_runs(self, registry, mock_storage) -> None:
        mock_storage.list_runs.return_value = [_make_run()]
        tool = registry.get_tool("list_cron_runs")
        result = tool(job_id="cj-abc123")
        assert result["success"] is True
        assert result["count"] == 1
        assert result["runs"][0]["child"] is None

    def test_list_runs_with_limit(self, registry, mock_storage) -> None:
        mock_storage.list_runs.return_value = []
        tool = registry.get_tool("list_cron_runs")
        tool(job_id="cj-abc123", limit=5)
        mock_storage.list_runs.assert_called_once_with("cj-abc123", limit=5)
        assert mock_storage.list_runs.call_count == 1
        assert mock_storage.list_runs.call_args is not None

    def test_list_runs_includes_child(self, registry, mock_storage) -> None:
        mock_storage.list_runs.return_value = [
            _make_run(
                status="dispatched",
                pipeline_execution_id="pe-child",
                child=CronRunChild(
                    type="pipeline_execution",
                    id="pe-child",
                    status="waiting_approval",
                    terminal=False,
                ),
            )
        ]
        tool = registry.get_tool("list_cron_runs")
        result = tool(job_id="cj-abc123")
        assert result["runs"][0]["child"] == {
            "type": "pipeline_execution",
            "id": "pe-child",
            "status": "waiting_approval",
            "terminal": False,
            "missing": False,
        }


class TestRunCronJobNow:
    @pytest.mark.asyncio
    async def test_run_now_with_scheduler(self, registry, mock_scheduler) -> None:
        mock_scheduler.run_now.return_value = _make_run()
        tool = registry.get_tool("run_cron_job")
        result = await tool(job_id="cj-abc123")
        assert result["success"] is True
        assert result["run"]["id"] == "cr-run123"

    @pytest.mark.asyncio
    async def test_run_now_not_found(self, registry, mock_scheduler, mock_storage) -> None:
        mock_scheduler.run_now.return_value = None
        mock_storage.get_job.return_value = None
        tool = registry.get_tool("run_cron_job")
        result = await tool(job_id="cj-nonexistent")
        assert result["success"] is False
        assert result["error_code"] == "cron_job_not_found"

    @pytest.mark.asyncio
    async def test_run_now_active_collision(self, registry, mock_scheduler, mock_storage) -> None:
        mock_scheduler.run_now.return_value = None
        mock_storage.get_job.return_value = _make_job()
        tool = registry.get_tool("run_cron_job")
        result = await tool(job_id="cj-abc123")
        assert result["success"] is False
        assert result["error_code"] == "cron_job_already_running"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [
            CronRunRejected(
                "cron_job_already_running",
                "Cron job already has a running run: cj-abc123",
            ),
            CronRunRejected(
                "cron_max_concurrent_jobs",
                "Cron scheduler is at max concurrency (1/1)",
            ),
        ],
    )
    async def test_run_now_rejection(
        self, registry, mock_scheduler, error: CronRunRejected
    ) -> None:
        mock_scheduler.run_now.side_effect = error
        tool = registry.get_tool("run_cron_job")
        result = await tool(job_id="cj-abc123")
        assert result["success"] is False
        assert result["error_code"] == error.code
        assert result["error"] == str(error)

    @pytest.mark.asyncio
    async def test_run_now_scheduler_unavailable_does_not_create_run(self, mock_storage) -> None:
        registry = create_cron_registry(cron_storage=mock_storage, cron_scheduler=None)
        tool = registry.get_tool("run_cron_job")
        result = await tool(job_id="cj-abc123")
        assert result == {
            "success": False,
            "error_code": "cron_scheduler_unavailable",
            "error": "Cron scheduler is not available",
        }
        mock_storage.create_run.assert_not_called()
