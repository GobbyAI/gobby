"""Tests for CronExecutor dispatch logic."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.scheduler.executor import CronExecutor
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob

if TYPE_CHECKING:
    from gobby.storage.database import LocalDatabase

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def cron_storage(temp_db: LocalDatabase) -> CronJobStorage:
    return CronJobStorage(temp_db)


@pytest.fixture
def executor(cron_storage: CronJobStorage) -> CronExecutor:
    return CronExecutor(storage=cron_storage)


def _make_job(storage: CronJobStorage, action_type: str, action_config: dict) -> CronJob:
    return storage.create_job(
        project_id=PROJECT_ID,
        name=f"Test {action_type}",
        schedule_type="cron",
        action_type=action_type,
        action_config=action_config,
        cron_expr="0 * * * *",
    )


@pytest.mark.asyncio
async def test_execute_shell_success(cron_storage: CronJobStorage, executor: CronExecutor) -> None:
    """Shell action runs command and captures output."""
    job = _make_job(cron_storage, "shell", {"command": "echo", "args": ["hello world"]})
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "completed"
    assert "hello world" in (result.output or "")


@pytest.mark.asyncio
async def test_execute_shell_timeout(cron_storage: CronJobStorage, executor: CronExecutor) -> None:
    """Shell action respects timeout."""
    job = _make_job(
        cron_storage,
        "shell",
        {"command": "sleep", "args": ["10"], "timeout_seconds": 1},
    )
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "failed"
    assert "timed out" in (result.error or "").lower()


@pytest.mark.asyncio
async def test_execute_shell_failure(cron_storage: CronJobStorage, executor: CronExecutor) -> None:
    """Shell action captures non-zero exit code."""
    job = _make_job(
        cron_storage,
        "shell",
        {"command": "false"},  # always exits with 1
    )
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "failed"
    assert result.error is not None


@pytest.mark.asyncio
async def test_execute_agent_spawn_no_runner(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    """agent_spawn without agent_runner raises error."""
    job = _make_job(
        cron_storage,
        "agent_spawn",
        {"prompt": "test", "provider": "claude"},
    )
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "failed"
    assert "not configured" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_agent_spawn_with_mock_runner(
    cron_storage: CronJobStorage,
) -> None:
    """agent_spawn delegates to agent_runner.spawn_headless."""
    mock_runner = MagicMock()
    mock_runner.spawn_headless = AsyncMock(return_value={"output": "Agent said hello"})
    executor = CronExecutor(storage=cron_storage, agent_runner=mock_runner)

    job = _make_job(
        cron_storage,
        "agent_spawn",
        {"prompt": "say hello", "provider": "claude", "timeout_seconds": 30},
    )
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "completed"
    assert "Agent said hello" in (result.output or "")
    mock_runner.spawn_headless.assert_called_once()


@pytest.mark.asyncio
async def test_execute_pipeline_no_executor(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    """pipeline without pipeline_executor raises error."""
    job = _make_job(
        cron_storage,
        "pipeline",
        {"pipeline_name": "test-pipeline"},
    )
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "failed"
    assert "not configured" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_unknown_action_type(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    """Unknown action_type returns error."""
    job = _make_job(cron_storage, "shell", {"command": "echo"})
    # Hack action_type to something invalid
    job.action_type = "unknown"  # type: ignore[assignment]
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "failed"
    assert "Unknown action_type" in (result.error or "")


@pytest.mark.asyncio
async def test_execute_updates_run_status(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    """Execute updates run to 'running' then 'completed'."""
    job = _make_job(cron_storage, "shell", {"command": "echo", "args": ["test"]})
    run = cron_storage.create_run(job.id)
    assert run.status == "pending"

    await executor.execute(job, run)
    # Fetch fresh from DB
    final = cron_storage.get_run(run.id)
    assert final is not None
    assert final.status == "completed"
    assert final.started_at is not None
    assert final.completed_at is not None


@pytest.mark.asyncio
async def test_execute_shell_missing_command(
    cron_storage: CronJobStorage, executor: CronExecutor
) -> None:
    """Shell action without command in config returns error."""
    job = _make_job(cron_storage, "shell", {"args": ["hello"]})
    run = cron_storage.create_run(job.id)

    result = await executor.execute(job, run)
    assert result.status == "failed"
    assert "command" in (result.error or "").lower()
