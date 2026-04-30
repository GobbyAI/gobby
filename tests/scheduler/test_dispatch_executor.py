"""Red tests for cron dispatcher action wiring."""

from __future__ import annotations

import pytest

from gobby.scheduler.executor import CronExecutor
from gobby.storage.cron import CronJobStorage

pytestmark = pytest.mark.unit


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

    temp_db.execute(
        "INSERT INTO projects (id, name, created_at, updated_at) "
        "VALUES (?, ?, datetime('now'), datetime('now'))",
        ("project-1", "Test Project"),
    )
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
