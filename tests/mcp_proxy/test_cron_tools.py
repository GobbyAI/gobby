"""Tests for cron MCP proxy tools."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.cron import create_cron_registry
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.scheduler.scheduler import CronRunRejected
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob, CronRun, CronRunChild
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.local_token import AgentApiTokenClaims
from gobby.utils.session_context import (
    reset_current_agent_run_id,
    reset_request_principal,
    set_current_agent_run_id,
    set_request_principal,
)

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000000"


def _agent_claims(marked: bool = False) -> AgentApiTokenClaims:
    return AgentApiTokenClaims(
        session_id="agent-session",
        project_id=PROJECT_ID,
        machine_id="local-machine",
        iat=1_700_000_000,
        exp=1_700_003_600,
        agent_run_id="agent-run" if marked else None,
    )


@contextmanager
def _caller(principal: AgentApiTokenClaims | None, *, marked: bool = False) -> Iterator[None]:
    async def resolve() -> AgentApiTokenClaims | None:
        return principal

    principal_token = set_request_principal(resolve)
    run_token = set_current_agent_run_id("agent-run") if marked else None
    try:
        yield
    finally:
        if run_token is not None:
            reset_current_agent_run_id(run_token)
        reset_request_principal(principal_token)


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


@pytest.fixture
def real_registry(temp_db, mock_scheduler: MagicMock) -> InternalToolRegistry:
    return create_cron_registry(
        cron_storage=CronJobStorage(temp_db),
        cron_scheduler=mock_scheduler,
    )


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
        result = asyncio.run(
            tool(
                name="Test",
                action_type="shell",
                action_config={"command": "echo"},
                cron_expr="0 7 * * *",
            )
        )
        assert result["success"] is True
        assert result["job"]["name"] == "Test Job"

    @pytest.mark.parametrize(
        ("schedule_type", "schedule"),
        [("cron", {"cron_expr": "invalid"}), ("once", {"run_at": "not-a-date"})],
    )
    def test_create_invalid_schedule_returns_error(
        self,
        real_registry: InternalToolRegistry,
        schedule_type: str,
        schedule: dict[str, object],
    ) -> None:
        tool = real_registry.get_tool("create_cron_job")

        result = asyncio.run(
            tool(
                name="Invalid",
                action_type="shell",
                action_config={"command": "echo"},
                project_id=PROJECT_ID,
                schedule_type=schedule_type,
                **schedule,
            )
        )

        assert result["success"] is False
        assert result["error"]


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

    def test_get_native_cli_cron_id_is_clean_not_found(
        self, real_registry: InternalToolRegistry, caplog: pytest.LogCaptureFixture
    ) -> None:
        """#22866: an 8-char CLI-native reminder id reaches no uuid cast or traceback."""
        tool = real_registry.get_tool("get_cron_job")
        assert tool is not None

        with caplog.at_level(logging.DEBUG, logger="gobby.mcp_proxy.tools.cron"):
            result = tool(job_id="84446b0d")

        assert result == {"success": False, "error": "Cron job not found: 84446b0d"}
        assert caplog.records == []


class TestUpdateCronJob:
    def test_update_success(self, registry, mock_storage) -> None:
        mock_storage.update_job.return_value = _make_job(name="Updated")
        tool = registry.get_tool("update_cron_job")
        result = asyncio.run(tool(job_id="cj-abc123", name="Updated"))
        assert result["success"] is True
        assert result["job"]["name"] == "Updated"

    def test_update_no_fields(self, registry, mock_storage) -> None:
        tool = registry.get_tool("update_cron_job")
        result = asyncio.run(tool(job_id="cj-abc123"))
        assert result["success"] is False
        assert "No fields" in result["error"]

    def test_update_not_found(self, registry, mock_storage) -> None:
        mock_storage.update_job.return_value = None
        tool = registry.get_tool("update_cron_job")
        result = asyncio.run(tool(job_id="cj-nonexistent", name="X"))
        assert result["success"] is False

    def test_update_reenables_disabled_job(self, real_registry: InternalToolRegistry) -> None:
        create = real_registry.get_tool("create_cron_job")
        update = real_registry.get_tool("update_cron_job")
        created = asyncio.run(
            create(
                name="Toggle",
                action_type="shell",
                action_config={"command": "echo"},
                project_id=PROJECT_ID,
                cron_expr="0 * * * *",
            )
        )
        job_id = created["job"]["id"]

        disabled = asyncio.run(update(job_id=job_id, enabled=False))
        enabled = asyncio.run(update(job_id=job_id, enabled=True))

        assert disabled["success"] is True
        assert disabled["job"]["next_run_at"] is None
        assert enabled["success"] is True
        assert enabled["job"]["next_run_at"] is not None


class TestToggleCronJob:
    def test_toggle_success(self, registry, mock_storage) -> None:
        mock_storage.toggle_job.return_value = _make_job(enabled=False)
        tool = registry.get_tool("toggle_cron_job")
        result = asyncio.run(tool(job_id="cj-abc123"))
        assert result["success"] is True
        assert result["state"] == "disabled"

    def test_toggle_not_found(self, registry, mock_storage) -> None:
        mock_storage.toggle_job.return_value = None
        tool = registry.get_tool("toggle_cron_job")
        result = asyncio.run(tool(job_id="cj-nonexistent"))
        assert result["success"] is False


class TestDeleteCronJob:
    def test_delete_success(self, registry, mock_storage) -> None:
        mock_storage.delete_job.return_value = True
        tool = registry.get_tool("delete_cron_job")
        result = asyncio.run(tool(job_id="cj-abc123"))
        assert result["success"] is True

    def test_delete_not_found(self, registry, mock_storage) -> None:
        mock_storage.delete_job.return_value = False
        tool = registry.get_tool("delete_cron_job")
        result = asyncio.run(tool(job_id="cj-nonexistent"))
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


@pytest.mark.asyncio
@pytest.mark.parametrize("marked", [False, True])
async def test_agent_cannot_create_shell_job(
    registry: InternalToolRegistry, mock_storage: MagicMock, marked: bool
) -> None:
    tool = registry.get_tool("create_cron_job")
    assert tool is not None
    with _caller(_agent_claims(marked), marked=marked):
        result = await tool(
            name="Escape",
            action_type="shell",
            action_config={"command": "echo"},
            cron_expr="0 7 * * *",
        )

    assert result["error_code"] == "forbidden"
    mock_storage.create_job.assert_not_called()


@pytest.mark.asyncio
async def test_agent_can_create_non_shell_job(
    registry: InternalToolRegistry, mock_storage: MagicMock
) -> None:
    mock_storage.create_job.return_value = _make_job(action_type="pipeline")
    tool = registry.get_tool("create_cron_job")
    assert tool is not None
    with _caller(_agent_claims()):
        result = await tool(
            name="Safe",
            action_type="pipeline",
            action_config={"pipeline": "safe"},
            cron_expr="0 7 * * *",
        )

    assert result["success"] is True
    mock_storage.create_job.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("marked", [False, True])
async def test_agent_cannot_convert_job_to_shell(
    registry: InternalToolRegistry, mock_storage: MagicMock, marked: bool
) -> None:
    tool = registry.get_tool("update_cron_job")
    assert tool is not None
    with _caller(_agent_claims(marked), marked=marked):
        result = await tool(
            job_id="cj-abc123", action_type="shell", action_config={"command": "echo"}
        )

    assert result["error_code"] == "forbidden"
    mock_storage.update_job.assert_not_called()
    mock_storage.update_non_shell_job.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [{"name": "Tampered"}, {"action_type": "pipeline", "action_config": {"pipeline": "safe"}}],
)
async def test_agent_cannot_update_existing_shell_job(
    real_registry: InternalToolRegistry, fields: dict[str, Any]
) -> None:
    create = real_registry.get_tool("create_cron_job")
    update = real_registry.get_tool("update_cron_job")
    get = real_registry.get_tool("get_cron_job")
    assert create is not None and update is not None and get is not None
    created = await create(
        name="Shell",
        action_type="shell",
        action_config={"command": "echo"},
        project_id=PROJECT_ID,
        cron_expr="0 7 * * *",
    )
    job_id = created["job"]["id"]

    with _caller(_agent_claims()):
        result = await update(job_id=job_id, **fields)

    assert result["error_code"] == "forbidden"
    unchanged = (await asyncio.to_thread(get, job_id=job_id))["job"]
    assert unchanged["name"] == "Shell"
    assert unchanged["action_type"] == "shell"


@pytest.mark.asyncio
async def test_agent_can_update_non_shell_job(
    registry: InternalToolRegistry, mock_storage: MagicMock
) -> None:
    mock_storage.update_non_shell_job.return_value = _make_job(
        action_type="pipeline", name="Updated"
    )
    tool = registry.get_tool("update_cron_job")
    assert tool is not None
    with _caller(_agent_claims()):
        result = await tool(job_id="cj-abc123", name="Updated")

    assert result["success"] is True
    mock_storage.update_non_shell_job.assert_called_once_with("cj-abc123", name="Updated")


@pytest.mark.asyncio
@pytest.mark.parametrize("marked", [False, True])
@pytest.mark.parametrize("tool_name", ["run_cron_job", "toggle_cron_job", "delete_cron_job"])
async def test_agent_cannot_run_toggle_or_delete_job(
    registry: InternalToolRegistry,
    mock_storage: MagicMock,
    mock_scheduler: MagicMock,
    marked: bool,
    tool_name: str,
) -> None:
    tool = registry.get_tool(tool_name)
    assert tool is not None
    with _caller(_agent_claims(marked), marked=marked):
        result = await tool(job_id="cj-abc123")

    assert result["error_code"] == "forbidden"
    mock_scheduler.run_now.assert_not_called()
    mock_storage.toggle_job.assert_not_called()
    mock_storage.delete_job.assert_not_called()


@pytest.mark.asyncio
async def test_operator_can_manage_shell_jobs(
    registry: InternalToolRegistry, mock_storage: MagicMock, mock_scheduler: MagicMock
) -> None:
    mock_storage.create_job.return_value = _make_job()
    mock_storage.update_job.return_value = _make_job(name="Updated")
    mock_storage.toggle_job.return_value = _make_job(enabled=False)
    mock_storage.delete_job.return_value = True
    mock_scheduler.run_now.return_value = _make_run()

    with _caller(None):
        created = await registry.call(
            "create_cron_job",
            {
                "name": "Shell",
                "action_type": "shell",
                "action_config": {"command": "echo"},
                "cron_expr": "0 7 * * *",
            },
        )
        updated = await registry.call(
            "update_cron_job", {"job_id": "cj-abc123", "action_type": "shell"}
        )
        toggled = await registry.call("toggle_cron_job", {"job_id": "cj-abc123"})
        deleted = await registry.call("delete_cron_job", {"job_id": "cj-abc123"})
        run = await registry.call("run_cron_job", {"job_id": "cj-abc123"})

    assert all(result["success"] for result in (created, updated, toggled, deleted, run))
    mock_storage.update_job.assert_called_once_with("cj-abc123", action_type="shell")


def test_agent_update_rechecks_action_at_write(
    temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = CronJobStorage(temp_db)
    job = storage.create_job(
        project_id=PROJECT_ID,
        name="Safe",
        schedule_type="cron",
        action_type="pipeline",
        action_config={"pipeline": "safe"},
        cron_expr="0 7 * * *",
    )
    normalize = storage._normalize_update_fields

    def switch_to_shell(current: CronJob, fields: dict[str, Any]) -> None:
        normalize(current, fields)
        storage.db.execute(
            "UPDATE cron_jobs SET action_type = 'shell' WHERE id = %s", (current.id,)
        )

    monkeypatch.setattr(storage, "_normalize_update_fields", switch_to_shell)
    with pytest.raises(PermissionError):
        storage.update_non_shell_job(job.id, name="Tampered")

    current = storage.get_job(job.id)
    assert current is not None
    assert current.action_type == "shell"
    assert current.name == "Safe"
