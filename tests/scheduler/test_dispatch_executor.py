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
    monkeypatch.setattr(dispatcher, "run_heartbeat", lambda **kwargs: calls.append(kwargs["project_id"]))

    storage = CronJobStorage(temp_db)
    job = storage.create_job(
        project_id="project-1",
        name="Dispatch heartbeat",
        schedule_type="interval",
        action_type="dispatcher",
        action_config={"project_id": "project-1"},
        interval_seconds=60,
    )
    run = storage.create_run(job.id, scheduled_time="2026-01-01T00:00:00+00:00")

    result = await CronExecutor(storage).execute(job, run)

    assert result.status == "success"
    assert calls == ["project-1"]

