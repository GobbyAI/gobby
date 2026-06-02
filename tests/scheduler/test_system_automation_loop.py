"""Tests for daemon-owned system automation loop."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.config.app import DaemonConfig, load_config
from gobby.scheduler.executor import CronExecutor
from gobby.scheduler.scheduler import CronScheduler
from gobby.storage.config_store import ConfigStore
from gobby.storage.cron import CronJobStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.system_automation import SystemAutomationLoop
from tests._timing import wait_for_async_condition

pytestmark = pytest.mark.unit


async def _run_inline(func: Any, *args: Any, **kwargs: Any) -> Any:
    return func(*args, **kwargs)


def _seed_project(db: HubDatabase, project_id: str = "project-1") -> None:
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


def test_config_store_overrides_system_automation_config(tmp_path) -> None:
    class DummyConfigStore:
        def get_all(self) -> dict[str, object]:
            return {
                "system_loops.automation.enabled": False,
                "system_loops.automation.interval_seconds": 7,
            }

    config = load_config(
        config_file=str(tmp_path / "bootstrap.yaml"),
        config_store=DummyConfigStore(),
    )

    assert config.system_loops.automation.enabled is False
    assert config.system_loops.automation.interval_seconds == 7


@pytest.mark.asyncio
async def test_config_store_overrides_runtime_loop_settings(temp_db: HubDatabase) -> None:
    store = ConfigStore(temp_db)
    loop = SystemAutomationLoop(
        db=temp_db,
        config=DaemonConfig(),
        config_store=store,
        run_db=_run_inline,
    )

    settings = await loop.resolve_settings()
    assert settings.enabled is True
    assert settings.interval_seconds == 60

    store.set("system_loops.automation.enabled", False)
    store.set("system_loops.automation.interval_seconds", 5)

    settings = await loop.resolve_settings()
    assert settings.enabled is False
    assert settings.interval_seconds == 5


@pytest.mark.asyncio
async def test_idle_automation_tick_creates_zero_cron_runs(temp_db: HubDatabase) -> None:
    loop = SystemAutomationLoop(
        db=temp_db,
        config=DaemonConfig(),
        run_db=_run_inline,
    )

    summary = await loop.run_once(reason="test")

    assert summary.projects == []
    assert temp_db.fetchone("SELECT COUNT(*) AS count FROM cron_runs")["count"] == 0


@pytest.mark.asyncio
async def test_eligible_project_work_calls_run_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
) -> None:
    from gobby.dispatch import dispatcher

    calls: list[str] = []
    monkeypatch.setattr(
        "gobby.system_automation.list_automation_candidates",
        lambda db: [SimpleNamespace(project_id="project-1")],
    )

    async def run_heartbeat(**kwargs: Any) -> dispatcher.HeartbeatResult:
        calls.append(kwargs["project_id"])
        return dispatcher.HeartbeatResult(scanned=1, executed=0, skipped=0)

    monkeypatch.setattr("gobby.system_automation.run_heartbeat", run_heartbeat)

    loop = SystemAutomationLoop(
        db=temp_db,
        config=DaemonConfig(),
        services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
        run_db=_run_inline,
    )

    summary = await loop.run_once(reason="test")

    assert calls == ["project-1"]
    assert summary.projects == ["project-1"]


@pytest.mark.asyncio
async def test_automation_tick_timeout_records_failure_and_releases_lock(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
) -> None:
    monkeypatch.setattr("gobby.system_automation.AUTOMATION_TICK_TIMEOUT_SECONDS", 0.01)
    loop = SystemAutomationLoop(
        db=temp_db,
        config=DaemonConfig(),
        services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
        run_db=_run_inline,
    )
    release = asyncio.Event()

    async def hang_pre_dispatch() -> object:
        await release.wait()
        return object()

    loop._run_pre_dispatch_maintenance = hang_pre_dispatch  # type: ignore[method-assign]

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
async def test_project_dispatch_timeout_returns_summary(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
) -> None:
    monkeypatch.setattr("gobby.system_automation.PROJECT_DISPATCH_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("gobby.system_automation.is_project_automation_enabled", lambda *_: True)
    monkeypatch.setattr("gobby.system_automation.recover_safe_build_claims", lambda *_: None)
    loop = SystemAutomationLoop(
        db=temp_db,
        config=DaemonConfig(),
        services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
        run_db=_run_inline,
    )
    release = asyncio.Event()

    async def run_heartbeat(**kwargs: Any) -> object:
        await release.wait()
        return object()

    monkeypatch.setattr("gobby.system_automation.run_heartbeat", run_heartbeat)

    summary = await loop.dispatch_project_once(project_id="project-1", reason="timeout-test")

    assert (
        summary.reason
        == "project_dispatch_timeout:project_id=project-1:reason=timeout-test:timeout=0.01s"
    )
    assert summary.ticks == 0
    assert loop.status_snapshot()["dispatch_count"] == 0
    release.set()


@pytest.mark.asyncio
async def test_multiple_projects_fan_out_independently(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
) -> None:
    from gobby.dispatch import dispatcher

    calls: list[str] = []
    monkeypatch.setattr(
        "gobby.system_automation.list_automation_candidates",
        lambda db: [
            SimpleNamespace(project_id="project-2"),
            SimpleNamespace(project_id="project-1"),
            SimpleNamespace(project_id="project-2"),
        ],
    )

    async def run_heartbeat(**kwargs: Any) -> dispatcher.HeartbeatResult:
        calls.append(kwargs["project_id"])
        return dispatcher.HeartbeatResult(scanned=1, executed=0, skipped=0)

    monkeypatch.setattr("gobby.system_automation.run_heartbeat", run_heartbeat)

    loop = SystemAutomationLoop(
        db=temp_db,
        config=DaemonConfig(),
        services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
        run_db=_run_inline,
    )

    summary = await loop.run_once(reason="test")

    assert calls == ["project-1", "project-2"]
    assert summary.projects == ["project-1", "project-2"]


@pytest.mark.asyncio
async def test_direct_wake_after_agent_cleanup_runs_project_heartbeat_without_cron_rows(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, str],
) -> None:
    from gobby.build.dispatch_tick import schedule_dispatcher_tick_for_task
    from gobby.dispatch import dispatcher
    from gobby.storage.tasks import LocalTaskManager

    calls: list[str] = []
    heartbeat_called = asyncio.Event()

    async def run_heartbeat(**kwargs: Any) -> dispatcher.HeartbeatResult:
        calls.append(kwargs["project_id"])
        heartbeat_called.set()
        return dispatcher.HeartbeatResult(scanned=1, executed=0, skipped=0)

    monkeypatch.setattr("gobby.dispatch.dispatcher.run_heartbeat", run_heartbeat)

    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Cleanup wake",
        category="code",
        task_type="feature",
    )
    manager.update_task(task.id, allow_automation=True)
    services = SimpleNamespace(
        agent_runner=object(),
        database=temp_db,
        startup_ready=True,
        shutdown_in_progress=False,
    )

    scheduled = schedule_dispatcher_tick_for_task(
        temp_db,
        task_id=task.id,
        reason="agent_cleanup",
        services=services,
    )

    assert scheduled is True
    await asyncio.wait_for(heartbeat_called.wait(), timeout=1)

    assert calls == [sample_project["id"]]
    assert temp_db.fetchone("SELECT COUNT(*) AS count FROM cron_runs")["count"] == 0
    assert CronJobStorage(temp_db).get_job_by_name("gobby:dispatcher") is None


@pytest.mark.asyncio
async def test_direct_project_dispatch_wake_queues_followup_when_dispatch_active(
    temp_db: HubDatabase,
) -> None:
    """A state-change wake during a running dispatch must not be dropped."""
    started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[str] = []

    loop = SystemAutomationLoop(
        db=temp_db,
        config=DaemonConfig(),
        services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
        run_db=_run_inline,
    )

    async def dispatch_project_once(**kwargs: Any) -> object:
        reason = str(kwargs["reason"])
        calls.append(reason)
        if reason == "first":
            started.set()
            await release_first.wait()
        return object()

    loop.dispatch_project_once = dispatch_project_once  # type: ignore[method-assign]

    assert loop.schedule_project_dispatch(project_id="project-1", reason="first") is True
    await asyncio.wait_for(started.wait(), timeout=1)

    assert loop.schedule_project_dispatch(project_id="project-1", reason="second") is True
    release_first.set()

    await wait_for_async_condition(
        lambda: calls == ["first", "second"],
        description="follow-up dispatch",
    )


@pytest.mark.asyncio
async def test_user_cron_jobs_still_create_cron_runs(temp_db: HubDatabase) -> None:
    _seed_project(temp_db)
    storage = CronJobStorage(temp_db)
    job = storage.create_job(
        project_id="project-1",
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
        config=DaemonConfig().cron,
    )

    await scheduler._check_due_jobs()
    if scheduler._active_tasks:
        await asyncio.gather(*list(scheduler._active_tasks), return_exceptions=True)

    runs = storage.list_runs(job.id)
    assert len(runs) == 1
    assert runs[0].status == "completed"
