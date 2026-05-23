"""Tests for CronScheduler background task logic."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.cron import CronConfig
from gobby.scheduler.executor import CronExecutor
from gobby.scheduler.scheduler import CronScheduler
from gobby.shutdown_intent import (
    ShutdownIntent,
    get_active_shutdown_marker_path,
    write_shutdown_intent,
)
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronRun
from tests._timing import drain_asyncio_tasks, wait_for_async_condition

if TYPE_CHECKING:
    from gobby.storage.database import LocalDatabase

PROJECT_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def cron_storage(temp_db: LocalDatabase) -> CronJobStorage:
    return CronJobStorage(temp_db)


@pytest.fixture
def mock_executor(cron_storage: CronJobStorage) -> CronExecutor:
    executor = CronExecutor(storage=cron_storage)

    def _complete_run(job: Any, run: CronRun) -> CronRun:
        """Helper to mark a run as completed."""
        now = datetime.now(UTC).isoformat()
        updated = cron_storage.update_run(run.id, status="completed", completed_at=now)
        return updated or run

    executor.execute = AsyncMock(side_effect=_complete_run)
    return executor


@pytest.fixture
def config() -> CronConfig:
    return CronConfig(check_interval_seconds=60, max_concurrent_jobs=5)


@pytest.fixture
def scheduler(
    cron_storage: CronJobStorage, mock_executor: CronExecutor, config: CronConfig
) -> CronScheduler:
    return CronScheduler(storage=cron_storage, executor=mock_executor, config=config)


@pytest.mark.asyncio
async def test_scheduler_advances_dispatcher_next_run_at(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    columns = {row["name"] for row in cron_storage.db.fetchall("PRAGMA table_info(cron_jobs)")}
    if "is_system" not in columns:
        cron_storage.db.execute(
            "ALTER TABLE cron_jobs ADD COLUMN is_system INTEGER NOT NULL DEFAULT 0"
        )
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="gobby:dispatcher",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "dispatch.tick"},
        interval_seconds=60,
    )
    cron_storage.db.execute("UPDATE cron_jobs SET is_system = 1 WHERE id = ?", (job.id,))
    cron_storage.update_system_job_bookkeeping(
        job.id,
        next_run_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )

    await scheduler._check_due_jobs()
    await wait_for_async_condition(
        lambda: (
            (updated := cron_storage.get_job(job.id)) is not None
            and updated.next_run_at is not None
            and updated.next_run_at != job.next_run_at
        ),
        description="dispatcher next_run_at update",
    )

    updated = cron_storage.get_job(job.id)
    assert updated is not None
    assert updated.next_run_at is not None
    assert updated.next_run_at != job.next_run_at


@pytest.mark.asyncio
async def test_start_creates_tasks(scheduler: CronScheduler) -> None:
    """start() creates check and cleanup tasks."""
    await scheduler.start()
    assert scheduler._running is True
    assert scheduler._check_task is not None
    assert scheduler._cleanup_task is not None
    await scheduler.stop()


@pytest.mark.asyncio
async def test_stop_cancels_tasks(scheduler: CronScheduler) -> None:
    """stop() cancels tasks gracefully."""
    await scheduler.start()
    await scheduler.stop()
    assert scheduler._running is False


@pytest.mark.asyncio
async def test_double_start_is_noop(scheduler: CronScheduler) -> None:
    """Calling start() twice doesn't create duplicate tasks."""
    await scheduler.start()
    task1 = scheduler._check_task
    await scheduler.start()  # Should be a no-op
    assert scheduler._check_task is task1
    await scheduler.stop()


@pytest.mark.asyncio
async def test_disabled_scheduler_does_not_start() -> None:
    """Scheduler doesn't start when config.enabled is False."""
    config = CronConfig(enabled=False)
    scheduler = CronScheduler(storage=MagicMock(), executor=MagicMock(), config=config)
    await scheduler.start()
    assert scheduler._running is False
    assert scheduler._check_task is None


@pytest.mark.asyncio
async def test_check_due_jobs_dispatches(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """_check_due_jobs dispatches due jobs to executor."""
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)

    # Create a job with next_run in the past
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Due Job",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    cron_storage.update_job(job.id, next_run_at=past)

    await scheduler._check_due_jobs()
    await wait_for_async_condition(
        lambda: mock_executor.execute.await_count >= 1,
        description="cron execution dispatch",
    )

    mock_executor.execute.assert_called_once()
    assert mock_executor.execute.call_count == 1
    assert mock_executor.execute.call_args is not None


@pytest.mark.asyncio
async def test_respects_max_concurrent(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
) -> None:
    """Scheduler respects max_concurrent_jobs limit."""
    config = CronConfig(check_interval_seconds=60, max_concurrent_jobs=1)
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)

    # Create a running run to fill the slot
    job1 = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Running",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    run = cron_storage.create_run(job1.id)
    cron_storage.update_run(run.id, status="running")

    # Create a due job
    job2 = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Waiting",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    cron_storage.update_job(job2.id, next_run_at=past)

    await scheduler._check_due_jobs()
    await drain_asyncio_tasks()

    # Should not have dispatched because max concurrent reached
    mock_executor.execute.assert_not_called()
    assert mock_executor.execute.call_count == 0
    assert not mock_executor.execute.called


@pytest.mark.asyncio
async def test_skips_job_with_active_run_but_dispatches_other_due_job(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
) -> None:
    """A long-running job cannot overlap itself, but other jobs can use free slots."""
    config = CronConfig(check_interval_seconds=60, max_concurrent_jobs=2)
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    active_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Active",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    active_run = cron_storage.create_run(active_job.id)
    cron_storage.update_run(active_run.id, status="running")
    cron_storage.update_job(active_job.id, next_run_at=past)
    waiting_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Waiting",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    cron_storage.update_job(waiting_job.id, next_run_at=past)

    await scheduler._check_due_jobs()
    await wait_for_async_condition(
        lambda: mock_executor.execute.await_count >= 1,
        description="dispatch non-overlapping cron job",
    )

    mock_executor.execute.assert_called_once()
    assert mock_executor.execute.await_args.args[0].id == waiting_job.id
    assert len(cron_storage.list_runs(active_job.id)) == 1


@pytest.mark.asyncio
async def test_due_jobs_run_heartbeat_then_dispatcher_then_others(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """Lifecycle cron jobs get deterministic priority when due together."""
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    names = ["other-job", "gobby:dispatcher", "gobby:pipeline-heartbeat"]
    for name in names:
        job = cron_storage.create_job(
            project_id=PROJECT_ID,
            name=name,
            schedule_type="interval",
            action_type="shell",
            action_config={"command": "echo"},
            interval_seconds=60,
        )
        cron_storage.update_job(job.id, next_run_at=past)

    await scheduler._check_due_jobs()
    await wait_for_async_condition(
        lambda: mock_executor.execute.await_count >= 3,
        description="priority cron dispatch",
    )

    dispatched = [call.args[0].name for call in mock_executor.execute.await_args_list]
    assert dispatched == ["gobby:pipeline-heartbeat", "gobby:dispatcher", "other-job"]


@pytest.mark.asyncio
async def test_stale_running_runs_do_not_block_due_jobs(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
) -> None:
    """Expired running rows are failed before concurrency slots are counted."""
    config = CronConfig(
        check_interval_seconds=60,
        max_concurrent_jobs=1,
        running_timeout_seconds=60,
    )
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)
    stale_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="stale",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    stale_run = cron_storage.create_run(stale_job.id)
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    cron_storage.update_run(stale_run.id, status="running", started_at=old)
    due_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="waiting",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    cron_storage.update_job(due_job.id, next_run_at=old)

    await scheduler._check_due_jobs()
    await wait_for_async_condition(
        lambda: mock_executor.execute.await_count >= 1,
        description="dispatch after stale cron cleanup",
    )

    refreshed_stale_run = cron_storage.get_run(stale_run.id)
    assert refreshed_stale_run is not None
    assert refreshed_stale_run.status == "failed"
    mock_executor.execute.assert_called_once()
    assert mock_executor.execute.await_args.args[0].id == due_job.id


@pytest.mark.asyncio
async def test_stale_running_runs_during_planned_restart_do_not_warn(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Expected restart recovery logs stale cron cleanup below warning severity."""
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    write_shutdown_intent("cli_restart", ShutdownIntent.RESTART, home=tmp_path)
    config = CronConfig(
        check_interval_seconds=60,
        max_concurrent_jobs=1,
        running_timeout_seconds=60,
    )
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)
    stale_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="stale",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    stale_run = cron_storage.create_run(stale_job.id)
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    cron_storage.update_run(stale_run.id, status="running", started_at=old)
    due_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="waiting",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    cron_storage.update_job(due_job.id, next_run_at=old)
    caplog.set_level("INFO", logger="gobby.scheduler.scheduler")

    await scheduler._check_due_jobs()
    await wait_for_async_condition(
        lambda: mock_executor.execute.await_count >= 1,
        description="dispatch after planned restart stale cron cleanup",
    )

    assert "during planned restart" in caplog.text
    assert "Marked 1 stale cron run(s) failed before dispatch" not in caplog.text
    warning_messages = [
        record.message for record in caplog.records if record.levelname == "WARNING"
    ]
    assert all("stale cron run" not in message for message in warning_messages)


@pytest.mark.asyncio
async def test_delayed_stale_running_runs_after_planned_restart_do_not_warn(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Restart recovery can lag until stale run timeout plus the next scheduler tick."""
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    write_shutdown_intent("cli_restart", ShutdownIntent.RESTART, home=tmp_path)
    active_marker = get_active_shutdown_marker_path(tmp_path)
    marker_data = json.loads(active_marker.read_text(encoding="utf-8"))
    marker_data["timestamp"] = time.time() - 150
    active_marker.write_text(json.dumps(marker_data), encoding="utf-8")
    config = CronConfig(
        check_interval_seconds=60,
        max_concurrent_jobs=1,
        running_timeout_seconds=60,
    )
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)
    stale_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="stale",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    stale_run = cron_storage.create_run(stale_job.id)
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    cron_storage.update_run(stale_run.id, status="running", started_at=old)
    due_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="waiting",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    cron_storage.update_job(due_job.id, next_run_at=old)
    caplog.set_level("INFO", logger="gobby.scheduler.scheduler")

    await scheduler._check_due_jobs()
    await wait_for_async_condition(
        lambda: mock_executor.execute.await_count >= 1,
        description="dispatch after delayed planned restart stale cron cleanup",
    )

    assert "during planned restart" in caplog.text
    assert "Marked 1 stale cron run(s) failed before dispatch" not in caplog.text
    warning_messages = [
        record.message for record in caplog.records if record.levelname == "WARNING"
    ]
    assert all("stale cron run" not in message for message in warning_messages)


@pytest.mark.asyncio
async def test_stale_running_runs_after_expired_planned_restart_window_warn(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unrelated stale cron cleanup outside restart recovery still warns."""
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    write_shutdown_intent("cli_restart", ShutdownIntent.RESTART, home=tmp_path)
    active_marker = get_active_shutdown_marker_path(tmp_path)
    marker_data = json.loads(active_marker.read_text(encoding="utf-8"))
    marker_data["timestamp"] = time.time() - 400
    active_marker.write_text(json.dumps(marker_data), encoding="utf-8")
    config = CronConfig(
        check_interval_seconds=60,
        max_concurrent_jobs=1,
        running_timeout_seconds=60,
    )
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)
    stale_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="stale",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    stale_run = cron_storage.create_run(stale_job.id)
    old = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    cron_storage.update_run(stale_run.id, status="running", started_at=old)
    due_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="waiting",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    cron_storage.update_job(due_job.id, next_run_at=old)
    caplog.set_level("INFO", logger="gobby.scheduler.scheduler")

    await scheduler._check_due_jobs()
    await wait_for_async_condition(
        lambda: mock_executor.execute.await_count >= 1,
        description="dispatch after expired restart stale cron cleanup",
    )

    assert "during planned restart" not in caplog.text
    warning_messages = [
        record.message for record in caplog.records if record.levelname == "WARNING"
    ]
    assert "Marked 1 stale cron run(s) failed before dispatch" in warning_messages


@pytest.mark.asyncio
async def test_backoff_on_consecutive_failures(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """Jobs with consecutive failures are skipped during backoff period."""
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)

    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Failing",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    # Set it as having failed recently with 2 consecutive failures
    now = datetime.now(UTC).isoformat()
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    cron_storage.update_job(
        job.id,
        next_run_at=past,
        last_run_at=now,  # Last run was just now
        consecutive_failures=2,  # 2nd failure -> 60s backoff
    )

    await scheduler._check_due_jobs()
    await drain_asyncio_tasks()

    # Should be skipped due to backoff
    mock_executor.execute.assert_not_called()
    assert mock_executor.execute.call_count == 0
    assert not mock_executor.execute.called


def test_get_backoff_seconds(scheduler: CronScheduler) -> None:
    """Backoff delays follow config pattern."""
    # Default delays: [30, 60, 300, 900, 3600]
    assert scheduler._get_backoff_seconds(1) == 30
    assert scheduler._get_backoff_seconds(2) == 60
    assert scheduler._get_backoff_seconds(3) == 300
    assert scheduler._get_backoff_seconds(5) == 3600
    assert scheduler._get_backoff_seconds(10) == 3600  # Capped at last value


@pytest.mark.asyncio
async def test_run_now(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """run_now triggers immediate execution."""
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)

    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Manual",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )

    run = await scheduler.run_now(job.id)
    assert run is not None
    assert run.cron_job_id == job.id

    await wait_for_async_condition(
        lambda: mock_executor.execute.await_count >= 1,
        description="manual cron execution",
    )
    mock_executor.execute.assert_called_once()


@pytest.mark.asyncio
async def test_run_now_executes_in_empty_session_context(
    cron_storage: CronJobStorage,
    config: CronConfig,
    temp_db: LocalDatabase,
    sample_project,
) -> None:
    """Manual cron ticks must not inherit the caller session context."""
    from gobby.mcp_proxy.tools.cron import create_cron_registry
    from gobby.storage.sessions import SessionManager
    from gobby.utils.project_context import get_project_context
    from gobby.utils.session_context import get_current_session_id, session_context_for_test
    from gobby.workflows.state_manager import SessionVariableManager, WorkflowInstanceManager

    executor = CronExecutor(storage=cron_storage)
    scheduler = CronScheduler(storage=cron_storage, executor=executor, config=config)
    seen: dict[str, object] = {}

    async def dispatch_tick_handler(job) -> str:
        current_session = get_current_session_id()
        seen["session_id"] = current_session
        project_ctx = get_project_context()
        seen["project_id"] = project_ctx.get("id") if project_ctx else None
        if current_session:
            SessionVariableManager(temp_db).set_variable(
                current_session,
                "_agent_type",
                "backend-developer",
            )
        return "tick"

    executor.register_handler("dispatch.tick", dispatch_tick_handler)
    job = cron_storage.create_job(
        project_id=sample_project["id"],
        name="gobby:dispatcher",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "dispatch.tick"},
        interval_seconds=60,
    )
    caller = SessionManager(temp_db).register(
        external_id="cron-caller",
        machine_id="machine",
        source="codex",
        project_id=sample_project["id"],
    )
    registry = create_cron_registry(cron_storage, scheduler)
    run_cron_job = registry.get_tool("run_cron_job")

    with session_context_for_test(caller.id):
        result = await run_cron_job(job.id)

    if scheduler._active_tasks:
        await asyncio.gather(*list(scheduler._active_tasks), return_exceptions=True)

    assert result["success"] is True
    assert seen == {"session_id": None, "project_id": sample_project["id"]}
    caller_vars = SessionVariableManager(temp_db).get_variables(caller.id)
    assert "_agent_type" not in caller_vars
    assert (
        WorkflowInstanceManager(temp_db).get_instance(caller.id, "backend-developer-steps") is None
    )


@pytest.mark.asyncio
async def test_run_now_nonexistent_job(scheduler: CronScheduler) -> None:
    """run_now returns None for non-existent job."""
    result = await scheduler.run_now("cj-nonexistent")
    assert result is None


@pytest.mark.asyncio
async def test_execute_and_update_success(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """_execute_and_update resets failure counter on success."""
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)

    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Success",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    cron_storage.update_job(job.id, consecutive_failures=3)

    run = cron_storage.create_run(job.id)
    await scheduler._execute_and_update(job, run)

    updated_job = cron_storage.get_job(job.id)
    assert updated_job is not None
    assert updated_job.consecutive_failures == 0
    assert updated_job.last_status == "completed"


@pytest.mark.asyncio
async def test_on_run_complete_callback_fires(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """on_run_complete callback fires after job execution with correct args."""
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)

    callback = AsyncMock()
    scheduler.on_run_complete = callback

    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Callback Test",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )

    run = cron_storage.create_run(job.id)
    await scheduler._execute_and_update(job, run)

    callback.assert_called_once()
    call_args = callback.call_args[0]
    assert call_args[0].id == job.id  # CronJob
    assert call_args[1].status == "completed"  # CronRun


@pytest.mark.asyncio
async def test_on_run_complete_callback_error_does_not_propagate(
    cron_storage: CronJobStorage,
    mock_executor: CronExecutor,
    config: CronConfig,
) -> None:
    """on_run_complete callback errors are swallowed (best-effort)."""
    scheduler = CronScheduler(storage=cron_storage, executor=mock_executor, config=config)

    callback = AsyncMock(side_effect=RuntimeError("callback exploded"))
    scheduler.on_run_complete = callback

    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Callback Error Test",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )

    run = cron_storage.create_run(job.id)
    # Should not raise despite callback error
    await scheduler._execute_and_update(job, run)

    callback.assert_called_once()
    # Job should still be updated correctly
    updated_job = cron_storage.get_job(job.id)
    assert updated_job is not None
    assert updated_job.last_status == "completed"


@pytest.mark.asyncio
async def test_on_run_complete_not_called_without_result(
    cron_storage: CronJobStorage,
    config: CronConfig,
) -> None:
    """on_run_complete not called when _execute_and_update gets no run."""
    executor = CronExecutor(storage=cron_storage)
    scheduler = CronScheduler(storage=cron_storage, executor=executor, config=config)

    callback = AsyncMock()
    scheduler.on_run_complete = callback

    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="No Run Test",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )

    # Pass None run — should bail early without calling callback
    await scheduler._execute_and_update(job, None)
    callback.assert_not_called()
    assert callback.call_count == 0
    assert not callback.called
