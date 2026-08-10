"""Tests for daemon-owned system automation loop."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.build.dispatch_tick import DispatcherTickSummary
from gobby.config.app import DaemonConfig, load_config
from gobby.scheduler.executor import CronExecutor
from gobby.scheduler.scheduler import CronScheduler
from gobby.storage.cron import CronJobStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.system_automation import SystemAutomationLoop
from tests._timing import wait_for_async_condition
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
        lambda db: [SimpleNamespace(project_id="11111111-1111-4111-8111-111111110001")],
    )

    async def run_heartbeat(**kwargs: Any) -> dispatcher.HeartbeatResult:
        calls.append(kwargs["project_id"])
        return dispatcher.HeartbeatResult(scanned=1, executed=0, skipped=0)

    monkeypatch.setattr("gobby.system_automation.run_heartbeat", run_heartbeat)

    loop = SystemAutomationLoop(
        db=temp_db,
        capture_bundle=static_runtime_capture(DaemonConfig()),
        services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
        run_db=_run_inline,
    )

    summary = await loop.run_once(reason="test")

    assert calls == ["11111111-1111-4111-8111-111111110001"]
    assert summary.projects == ["11111111-1111-4111-8111-111111110001"]


@pytest.mark.parametrize(
    ("heartbeat_result", "expected_level"),
    [
        (DispatcherTickSummary(), logging.DEBUG),
        (DispatcherTickSummary(executed=1), logging.INFO),
        (DispatcherTickSummary(cap_reached=True, reason="cap_reached"), logging.INFO),
        (DispatcherTickSummary(reason="dispatcher_unavailable"), logging.INFO),
    ],
)
def test_project_dispatch_summary_level_tracks_operational_events(
    heartbeat_result: DispatcherTickSummary,
    expected_level: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gobby.system_automation import _log_project_dispatch_summary

    caplog.set_level(logging.DEBUG, logger="gobby.system_automation")

    _log_project_dispatch_summary(
        project_id="project-1",
        trigger_reason="test",
        summary=heartbeat_result,
    )

    record = next(
        record
        for record in caplog.records
        if record.message == "system_automation_project_dispatch"
    )
    assert record.levelno == expected_level
    assert record.project_id == "project-1"
    assert record.trigger_reason == "test"
    assert record.executed == heartbeat_result.executed
    assert record.cap_reached == heartbeat_result.cap_reached


@pytest.mark.parametrize(
    ("heartbeat_result", "expected_level"),
    [
        (DispatcherTickSummary(), logging.DEBUG),
        (DispatcherTickSummary(executed=1), logging.INFO),
        (DispatcherTickSummary(cap_reached=True, reason="cap_reached"), logging.INFO),
        (DispatcherTickSummary(reason="dispatcher_unavailable"), logging.INFO),
    ],
)
def test_build_dispatch_summary_level_tracks_operational_events(
    heartbeat_result: DispatcherTickSummary,
    expected_level: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from gobby.build.dispatch_tick import _log_dispatcher_tick_summary

    caplog.set_level(logging.DEBUG, logger="gobby.build.dispatch_tick")

    _log_dispatcher_tick_summary(project_id="project-1", summary=heartbeat_result)

    record = next(
        record for record in caplog.records if record.message == "dispatcher_tick_summary"
    )
    assert record.levelno == expected_level
    assert record.project_id == "project-1"
    assert record.executed == heartbeat_result.executed
    assert record.cap_reached == heartbeat_result.cap_reached


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
        capture_bundle=static_runtime_capture(DaemonConfig()),
        services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
        run_db=_run_inline,
    )
    release = asyncio.Event()

    async def run_heartbeat(**kwargs: Any) -> object:
        await release.wait()
        return object()

    monkeypatch.setattr("gobby.system_automation.run_heartbeat", run_heartbeat)

    summary = await loop.dispatch_project_once(
        project_id="11111111-1111-4111-8111-111111110001", reason="timeout-test"
    )

    assert summary.reason == (
        "project_dispatch_timeout:project_id=11111111-1111-4111-8111-111111110001"
        ":reason=timeout-test:timeout=0.01s"
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
            SimpleNamespace(project_id="11111111-1111-4111-8111-111111110002"),
            SimpleNamespace(project_id="11111111-1111-4111-8111-111111110001"),
            SimpleNamespace(project_id="11111111-1111-4111-8111-111111110002"),
        ],
    )

    async def run_heartbeat(**kwargs: Any) -> dispatcher.HeartbeatResult:
        calls.append(kwargs["project_id"])
        return dispatcher.HeartbeatResult(scanned=1, executed=0, skipped=0)

    monkeypatch.setattr("gobby.system_automation.run_heartbeat", run_heartbeat)

    loop = SystemAutomationLoop(
        db=temp_db,
        capture_bundle=static_runtime_capture(DaemonConfig()),
        services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
        run_db=_run_inline,
    )

    summary = await loop.run_once(reason="test")

    assert calls == ["11111111-1111-4111-8111-111111110001", "11111111-1111-4111-8111-111111110002"]
    assert summary.projects == [
        "11111111-1111-4111-8111-111111110001",
        "11111111-1111-4111-8111-111111110002",
    ]


@pytest.mark.asyncio
async def test_dispatch_projects_isolates_project_failure(
    caplog: pytest.LogCaptureFixture,
    temp_db: HubDatabase,
) -> None:
    failed_project = "11111111-1111-4111-8111-111111110001"
    successful_project = "11111111-1111-4111-8111-111111110002"
    completed: list[str] = []
    loop = SystemAutomationLoop(
        db=temp_db, capture_bundle=static_runtime_capture(DaemonConfig()), run_db=_run_inline
    )

    async def dispatch_project_once(**kwargs: Any) -> DispatcherTickSummary:
        project_id = str(kwargs["project_id"])
        if project_id == failed_project:
            raise RuntimeError("project dispatch failed")
        completed.append(project_id)
        return DispatcherTickSummary(reason=str(kwargs["reason"]))

    loop.dispatch_project_once = dispatch_project_once  # type: ignore[method-assign]

    results = await loop._dispatch_projects(
        [failed_project, successful_project],
        reason="interval",
    )

    assert results == {successful_project: DispatcherTickSummary(reason="interval")}
    assert completed == [successful_project]
    assert f"System automation dispatch failed for project {failed_project}" in caplog.text


@pytest.mark.asyncio
async def test_dispatch_projects_propagates_project_cancellation(temp_db: HubDatabase) -> None:
    project_id = "11111111-1111-4111-8111-111111110001"
    loop = SystemAutomationLoop(
        db=temp_db, capture_bundle=static_runtime_capture(DaemonConfig()), run_db=_run_inline
    )

    async def dispatch_project_once(**_kwargs: Any) -> DispatcherTickSummary:
        raise asyncio.CancelledError

    loop.dispatch_project_once = dispatch_project_once  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await loop._dispatch_projects([project_id], reason="interval")


@pytest.mark.asyncio
async def test_overlapping_interval_dispatches_serialize_and_run_followup(
    temp_db: HubDatabase,
) -> None:
    project_id = "11111111-1111-4111-8111-111111110001"
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    max_active = 0
    reasons: list[str] = []
    loop = SystemAutomationLoop(
        db=temp_db, capture_bundle=static_runtime_capture(DaemonConfig()), run_db=_run_inline
    )

    async def dispatch_project_once(**kwargs: Any) -> DispatcherTickSummary:
        nonlocal active, max_active
        reason = str(kwargs["reason"])
        reasons.append(reason)
        active += 1
        max_active = max(max_active, active)
        try:
            if reason == "interval-one":
                first_started.set()
                await release_first.wait()
            return DispatcherTickSummary(reason=reason)
        finally:
            active -= 1

    loop.dispatch_project_once = dispatch_project_once  # type: ignore[method-assign]

    first = asyncio.create_task(loop._dispatch_projects([project_id], reason="interval-one"))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second = asyncio.create_task(loop._dispatch_projects([project_id], reason="interval-two"))
    await wait_for_async_condition(
        lambda: project_id in loop._pending_project_dispatches,
        description="coalesced interval follow-up",
    )

    assert reasons == ["interval-one"]
    release_first.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert reasons == ["interval-one", "interval-two"]
    assert max_active == 1
    assert first_result[project_id].reason == "interval-one"
    assert second_result[project_id].reason == "interval-two"


@pytest.mark.asyncio
async def test_interval_dispatches_for_different_projects_run_concurrently(
    temp_db: HubDatabase,
) -> None:
    project_ids = [
        "11111111-1111-4111-8111-111111110001",
        "11111111-1111-4111-8111-111111110002",
    ]
    started = {project_id: asyncio.Event() for project_id in project_ids}
    release = asyncio.Event()
    active = 0
    max_active = 0
    loop = SystemAutomationLoop(
        db=temp_db, capture_bundle=static_runtime_capture(DaemonConfig()), run_db=_run_inline
    )

    async def dispatch_project_once(**kwargs: Any) -> DispatcherTickSummary:
        nonlocal active, max_active
        project_id = str(kwargs["project_id"])
        active += 1
        max_active = max(max_active, active)
        started[project_id].set()
        try:
            await release.wait()
            return DispatcherTickSummary(reason=str(kwargs["reason"]))
        finally:
            active -= 1

    loop.dispatch_project_once = dispatch_project_once  # type: ignore[method-assign]
    dispatch = asyncio.create_task(loop._dispatch_projects(project_ids, reason="interval"))

    await asyncio.wait_for(
        asyncio.gather(*(event.wait() for event in started.values())),
        timeout=1,
    )
    assert max_active == 2
    release.set()

    results = await dispatch
    assert set(results) == set(project_ids)


@pytest.mark.asyncio
async def test_cancelled_interval_dispatch_cancels_owned_project_task(
    temp_db: HubDatabase,
) -> None:
    project_id = "11111111-1111-4111-8111-111111110001"
    started = asyncio.Event()
    cancelled = asyncio.Event()
    loop = SystemAutomationLoop(
        db=temp_db, capture_bundle=static_runtime_capture(DaemonConfig()), run_db=_run_inline
    )

    async def dispatch_project_once(**_kwargs: Any) -> DispatcherTickSummary:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    loop.dispatch_project_once = dispatch_project_once  # type: ignore[method-assign]
    dispatch = asyncio.create_task(loop._dispatch_projects([project_id], reason="interval"))
    await asyncio.wait_for(started.wait(), timeout=1)

    dispatch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dispatch

    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await wait_for_async_condition(
        lambda: project_id not in loop._project_tasks,
        description="cancelled interval project task cleanup",
    )


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
        validation_criteria="Test task completion is observable.",
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
        capture_bundle=static_runtime_capture(DaemonConfig()),
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
    loop._running = True

    assert (
        loop.schedule_project_dispatch(
            project_id="11111111-1111-4111-8111-111111110001", reason="first"
        )
        is True
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    assert (
        loop.schedule_project_dispatch(
            project_id="11111111-1111-4111-8111-111111110001", reason="second"
        )
        is True
    )
    release_first.set()

    await wait_for_async_condition(
        lambda: calls == ["first", "second"],
        description="follow-up dispatch",
    )


@pytest.mark.asyncio
async def test_queued_targeted_dispatches_merge_task_scope(
    temp_db: HubDatabase,
) -> None:
    """Coalesced continuations must retain every explicitly authorized task."""
    started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[tuple[str, tuple[str, ...] | None]] = []

    loop = SystemAutomationLoop(
        db=temp_db,
        capture_bundle=static_runtime_capture(DaemonConfig()),
        services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
        run_db=_run_inline,
    )

    async def dispatch_project_once(**kwargs: Any) -> object:
        reason = str(kwargs["reason"])
        calls.append((reason, kwargs["explicit_task_ids"]))
        if reason == "first":
            started.set()
            await release_first.wait()
        return object()

    loop.dispatch_project_once = dispatch_project_once  # type: ignore[method-assign]
    loop._running = True
    project_id = "11111111-1111-4111-8111-111111110001"

    assert loop.schedule_project_dispatch(
        project_id=project_id,
        reason="first",
        explicit_task_ids=("task-a",),
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    assert loop.schedule_project_dispatch(
        project_id=project_id,
        reason="second",
        explicit_task_ids=("task-b",),
    )
    assert loop.schedule_project_dispatch(
        project_id=project_id,
        reason="third",
        explicit_task_ids=("task-c", "task-b"),
    )
    release_first.set()

    await wait_for_async_condition(
        lambda: calls
        == [
            ("first", ("task-a",)),
            ("third", ("task-b", "task-c")),
        ],
        description="merged targeted follow-up dispatch",
    )


async def test_project_dispatch_entrypoints_ignore_requests_after_stop(
    temp_db: HubDatabase,
) -> None:
    event_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    event_loop.is_closed.return_value = False
    loop = SystemAutomationLoop(
        db=temp_db,
        capture_bundle=static_runtime_capture(DaemonConfig()),
        services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
        run_db=_run_inline,
    )
    loop._event_loop = event_loop
    loop._running = True

    await loop.stop()

    assert loop.schedule_project_dispatch(project_id="project", reason="stopped") is False
    loop._schedule_project_dispatch_on_loop("project", "stopped", None, None, None)

    event_loop.call_soon_threadsafe.assert_not_called()
    assert loop._project_tasks == {}


async def test_queued_project_dispatch_callback_does_no_work_after_stop(
    temp_db: HubDatabase,
) -> None:
    queued: tuple[Callable[..., None], tuple[object, ...]] | None = None

    def capture_callback(callback: Callable[..., None], *args: object) -> None:
        nonlocal queued
        queued = (callback, args)

    event_loop = MagicMock(spec=asyncio.AbstractEventLoop)
    event_loop.is_closed.return_value = False
    event_loop.call_soon_threadsafe.side_effect = capture_callback
    loop = SystemAutomationLoop(
        db=temp_db,
        capture_bundle=static_runtime_capture(DaemonConfig()),
        services=SimpleNamespace(startup_ready=True, shutdown_in_progress=False),
        run_db=_run_inline,
    )
    dispatch_project_once = AsyncMock()
    loop.dispatch_project_once = dispatch_project_once  # type: ignore[method-assign]
    loop._event_loop = event_loop
    loop._running = True

    assert loop.schedule_project_dispatch(project_id="project", reason="race") is True
    assert queued is not None

    await loop.stop()
    callback, args = queued
    callback(*args)

    assert loop._project_tasks == {}
    assert loop._pending_project_dispatches == {}
    dispatch_project_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_cron_jobs_still_create_cron_runs(temp_db: HubDatabase) -> None:
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

    await scheduler._check_due_jobs(scheduler._capture_config())
    if scheduler._active_tasks:
        await asyncio.gather(*list(scheduler._active_tasks), return_exceptions=True)

    runs = storage.list_runs(job.id)
    assert len(runs) == 1
    assert runs[0].status == "completed"
