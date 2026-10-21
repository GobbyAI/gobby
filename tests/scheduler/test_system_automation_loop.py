"""Tests for daemon-owned system automation loop."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from gobby.config.app import DaemonConfig
from gobby.scheduler.executor import CronExecutor
from gobby.scheduler.scheduler import CronScheduler
from gobby.storage.config_repository import ConfigRepository
from gobby.storage.cron import CronJobStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import LocalProjectCheckoutManager
from gobby.system_automation import SystemAutomationLoop
from tests.config_runtime_helpers import static_cron_capture, static_runtime_capture

pytestmark = pytest.mark.unit


async def _run_inline(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


@pytest.mark.asyncio
async def test_pipeline_maintenance_uses_single_stale_task_scan(temp_db: HubDatabase) -> None:
    class Heartbeat:
        stale_task_scans = 0

        async def check_stalled_executions(self) -> int:
            return 2

        async def check_stale_tasks(self) -> tuple[int, int]:
            self.stale_task_scans += 1
            return 3, 4

        async def count_running_executions(self) -> int:
            return 5

    heartbeat = Heartbeat()
    loop = SystemAutomationLoop(
        db=temp_db,
        capture_bundle=static_runtime_capture(DaemonConfig()),
        pipeline_heartbeat=heartbeat,
        run_db=_run_inline,
    )

    summary = await loop._run_pipeline_maintenance()

    assert heartbeat.stale_task_scans == 1
    assert summary.pipeline_stalled_handled == 2
    assert summary.pipeline_stale_tasks_recovered == 3
    assert summary.pipeline_stale_task_candidates == 4
    assert summary.pipeline_running_executions == 5
    snapshot = loop.status_snapshot()
    assert "dispatch_count" not in snapshot
    assert "pending_projects" not in snapshot
    assert "project_dispatch_timeout_seconds" not in snapshot


def _seed_project(
    db: HubDatabase, project_id: str = "11111111-1111-4111-8111-111111110001"
) -> None:
    db.execute(
        """
        INSERT INTO projects (id, name, created_at, updated_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO NOTHING
        """,
        (project_id, f"Project {project_id}"),
    )


def test_system_automation_config_defaults_enabled_with_sixty_second_interval() -> None:
    config = DaemonConfig()

    assert config.system_loops.automation.enabled is True
    assert config.system_loops.automation.interval_seconds == 60


def test_runtime_candidate_overrides_system_automation_config() -> None:
    config = ConfigRepository(MagicMock()).runtime_candidate(
        {
            "system_loops.automation.enabled": False,
            "system_loops.automation.interval_seconds": 7,
        },
        {},
    )

    assert config.system_loops.automation.enabled is False
    assert config.system_loops.automation.interval_seconds == 7


def test_runtime_bundle_updates_loop_settings(temp_db: HubDatabase) -> None:
    bundles = iter(
        [
            static_runtime_capture(DaemonConfig())(),
            static_runtime_capture(
                DaemonConfig(system_loops={"automation": {"enabled": False, "interval_seconds": 5}})
            )(),
        ]
    )
    loop = SystemAutomationLoop(
        db=temp_db,
        capture_bundle=lambda: next(bundles),
        run_db=_run_inline,
    )

    settings = loop.resolve_settings()
    assert settings.enabled is True
    assert settings.interval_seconds == 60

    settings = loop.resolve_settings()
    assert settings.enabled is False
    assert settings.interval_seconds == 5


@pytest.mark.asyncio
async def test_idle_automation_tick_creates_zero_cron_runs(temp_db: HubDatabase) -> None:
    loop = SystemAutomationLoop(
        db=temp_db,
        capture_bundle=static_runtime_capture(DaemonConfig()),
        run_db=_run_inline,
    )

    summary = await loop.run_once(reason="test")

    assert summary.error is None
    assert "projects" not in vars(summary)
    assert "dispatch" not in vars(summary)
    cron_count = temp_db.fetchone("SELECT COUNT(*) AS count FROM cron_runs")
    assert cron_count is not None
    assert cron_count["count"] == 0


@pytest.mark.asyncio
async def test_automation_tick_timeout_records_failure_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
) -> None:
    monkeypatch.setattr("gobby.system_automation.AUTOMATION_TICK_TIMEOUT_SECONDS", 0.01)
    loop = SystemAutomationLoop(
        db=temp_db,
        capture_bundle=static_runtime_capture(DaemonConfig()),
        services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
        run_db=_run_inline,
    )
    release = asyncio.Event()

    async def hang_claim_maintenance() -> int:
        await release.wait()
        return 0

    monkeypatch.setattr(loop, "_run_claim_maintenance", hang_claim_maintenance)

    summary = await loop.run_once(reason="timeout-test")

    assert summary.error == "automation_tick_timeout:0.01s"
    status = loop.status_snapshot()
    assert status["last_error"] == "automation_tick_timeout:0.01s"
    assert status["last_tick"]["error"] == "automation_tick_timeout:0.01s"
    assert status["tick_count"] == 1

    release.set()

    async with asyncio.timeout(1):
        async with loop._tick_lock:
            pass


@pytest.mark.asyncio
async def test_user_cron_jobs_still_create_cron_runs(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _seed_project(temp_db)
    storage = CronJobStorage(temp_db)
    job = storage.create_job(
        project_id="11111111-1111-4111-8111-111111110001",
        name="user-shell",
        schedule_type="interval",
        interval_seconds=60,
        action_type="shell",
        action_config={
            "command": sys.executable,
            "args": ["-c", "print('ok')"],
        },
    )
    storage.update_job(
        job.id,
        next_run_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )
    scheduler = CronScheduler(
        storage=storage,
        executor=CronExecutor(storage),
        capture_bundle=static_cron_capture(DaemonConfig().cron),
    )
    LocalProjectCheckoutManager(temp_db).register(
        scheduler._machine_id,
        job.project_id,
        str(tmp_path),
    )
    dispatched_tasks: list[asyncio.Task[None]] = []
    track_run_task = scheduler._track_run_task

    def capture_run_task(task: asyncio.Task[None], run_id: str) -> None:
        dispatched_tasks.append(task)
        track_run_task(task, run_id)

    monkeypatch.setattr(scheduler, "_track_run_task", capture_run_task)

    await scheduler._check_due_jobs(scheduler._capture_config())
    assert len(dispatched_tasks) == 1
    assert await asyncio.gather(*dispatched_tasks) == [None]

    runs = storage.list_runs(job.id)
    assert len(runs) == 1
    assert runs[0].status == "completed"


@pytest.mark.asyncio
async def test_cron_context_failure_terminalizes_run(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_project(temp_db)
    storage = CronJobStorage(temp_db)
    job = storage.create_job(
        project_id="11111111-1111-4111-8111-111111110001",
        name="missing-checkout",
        schedule_type="interval",
        interval_seconds=60,
        action_type="shell",
        action_config={"command": sys.executable, "args": ["-c", "print('unreachable')"]},
    )
    storage.update_job(
        job.id,
        next_run_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )
    scheduler = CronScheduler(
        storage=storage,
        executor=CronExecutor(storage),
        capture_bundle=static_cron_capture(DaemonConfig().cron),
    )
    dispatched_tasks: list[asyncio.Task[None]] = []
    track_run_task = scheduler._track_run_task

    def capture_run_task(task: asyncio.Task[None], run_id: str) -> None:
        dispatched_tasks.append(task)
        track_run_task(task, run_id)

    monkeypatch.setattr(scheduler, "_track_run_task", capture_run_task)

    await scheduler._check_due_jobs(scheduler._capture_config())
    assert len(dispatched_tasks) == 1
    assert await asyncio.gather(*dispatched_tasks) == [None]

    runs = storage.list_runs(job.id)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].error is not None
    assert "no checkout" in runs[0].error
