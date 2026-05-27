"""Red tests for cron dispatcher action wiring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from gobby.scheduler.executor import CronExecutor
from gobby.storage.cron import CronJobStorage
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


def _seed_project(temp_db: HubDatabase) -> None:
    now = datetime.now(UTC).isoformat()
    temp_db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) VALUES (%s, %s, %s, %s)",
        ("project-1", "Test Project", now, now),
    )


async def test_dispatcher_action_invokes_run_heartbeat(
    temp_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.dispatch import dispatcher

    calls: list[str] = []

    async def run_heartbeat(**kwargs):
        calls.append(kwargs["project_id"])
        return dispatcher.HeartbeatResult(scanned=1, executed=0, skipped=1)

    monkeypatch.setattr(dispatcher, "run_heartbeat", run_heartbeat)

    _seed_project(temp_db)
    storage = CronJobStorage(temp_db)
    job = storage.create_job(
        project_id="project-1",
        name="Dispatch heartbeat",
        schedule_type="interval",
        action_type="dispatcher",
        action_config={"project_id": "project-1"},
        interval_seconds=60,
    )
    run = storage.create_run(job.id)

    result = await CronExecutor(storage).execute(job, run)

    assert result.status == "completed"
    assert calls == ["project-1"]


@pytest.mark.asyncio
async def test_idle_dispatcher_cron_run_parks_system_job(
    temp_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.dispatch import dispatcher

    async def run_heartbeat(**_kwargs):
        return dispatcher.HeartbeatResult(scanned=0, executed=0, skipped=0)

    monkeypatch.setattr(dispatcher, "run_heartbeat", run_heartbeat)

    _seed_project(temp_db)
    storage = CronJobStorage(temp_db)
    job = storage.create_job(
        project_id="project-1",
        name="gobby:dispatcher",
        schedule_type="interval",
        action_type="dispatcher",
        action_config={"project_id": "project-1"},
        interval_seconds=60,
        is_system=True,
    )
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    storage.update_system_job_bookkeeping(job.id, next_run_at=future)
    run = storage.create_run(job.id)

    result = await CronExecutor(storage).execute(job, run)

    assert result.status == "completed"
    parked = storage.get_job(job.id)
    assert parked is not None
    assert parked.enabled is True
    assert parked.next_run_at is None


@pytest.mark.asyncio
async def test_dispatcher_cron_run_with_work_does_not_park(
    temp_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.dispatch import dispatcher

    calls = 0

    async def run_heartbeat(**_kwargs):
        nonlocal calls
        calls += 1
        return dispatcher.HeartbeatResult(scanned=1, executed=1, skipped=0)

    monkeypatch.setattr(dispatcher, "run_heartbeat", run_heartbeat)

    _seed_project(temp_db)
    storage = CronJobStorage(temp_db)
    job = storage.create_job(
        project_id="project-1",
        name="gobby:dispatcher",
        schedule_type="interval",
        action_type="dispatcher",
        action_config={"project_id": "project-1"},
        interval_seconds=60,
        is_system=True,
    )
    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    storage.update_system_job_bookkeeping(job.id, next_run_at=future)
    run = storage.create_run(job.id)

    result = await CronExecutor(storage).execute(job, run)

    assert result.status == "completed"
    assert calls == 3
    assert result.output is not None
    assert "ticks=3" in result.output
    assert "scanned=3" in result.output
    assert "executed=3" in result.output
    scheduled = storage.get_job(job.id)
    assert scheduled is not None
    assert scheduled.next_run_at == future


@pytest.mark.asyncio
async def test_dispatcher_cron_burst_aggregates_reason_and_cap(
    temp_db,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.dispatch import dispatcher

    results = [
        dispatcher.HeartbeatResult(scanned=2, executed=1, skipped=1),
        dispatcher.HeartbeatResult(
            scanned=3,
            executed=1,
            skipped=0,
            cap_reached=True,
            reason="spawn_unavailable",
        ),
        dispatcher.HeartbeatResult(scanned=99, executed=99, skipped=99),
    ]

    async def run_heartbeat(**_kwargs):
        return results.pop(0)

    monkeypatch.setattr(dispatcher, "run_heartbeat", run_heartbeat)

    _seed_project(temp_db)
    storage = CronJobStorage(temp_db)
    job = storage.create_job(
        project_id="project-1",
        name="gobby:dispatcher",
        schedule_type="interval",
        action_type="dispatcher",
        action_config={"project_id": "project-1"},
        interval_seconds=60,
        is_system=True,
    )
    run = storage.create_run(job.id)

    result = await CronExecutor(storage).execute(job, run)

    assert result.status == "completed"
    assert result.output is not None
    assert "ticks=2" in result.output
    assert "scanned=5" in result.output
    assert "executed=2" in result.output
    assert "skipped=1" in result.output
    assert "cap_reached=True" in result.output
    assert "reason=spawn_unavailable" in result.output
    assert len(results) == 1


async def test_disabled_dispatcher_tick_reports_hard_stop(temp_db) -> None:
    from gobby.build.dispatch_tick import kick_dispatcher_tick
    from gobby.runner import install_dispatcher_cron_row

    _seed_project(temp_db)
    storage = CronJobStorage(temp_db)
    job = install_dispatcher_cron_row(temp_db, project_id="project-1")
    storage.update_job(job.id, enabled=False)
    storage.update_system_job_bookkeeping(job.id, next_run_at=None)

    summary = await kick_dispatcher_tick(db=temp_db, project_id="project-1")

    assert summary.ticks == 0
    assert summary.reason == "dispatcher_cron_disabled"
    stopped = storage.get_job(job.id)
    assert stopped is not None
    assert stopped.enabled is False
    assert stopped.next_run_at is None
