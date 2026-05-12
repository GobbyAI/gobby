"""Red tests for project-wide build stop/resume."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_stop_disables_dispatcher_cron(temp_db) -> None:
    from gobby.build.service import build_stop
    from gobby.runner import DISPATCHER_CRON_JOB_NAME
    from gobby.storage.cron import CronJobStorage

    result = build_stop(db=temp_db, project_id="project-1")

    assert result.enabled is False
    assert CronJobStorage(temp_db).get_job_by_name(DISPATCHER_CRON_JOB_NAME).enabled is False


def test_resume_enables_dispatcher_cron(temp_db) -> None:
    from gobby.build.service import build_resume, build_stop
    from gobby.runner import DISPATCHER_CRON_JOB_NAME
    from gobby.storage.cron import CronJobStorage

    build_stop(db=temp_db, project_id="project-1")

    result = build_resume(db=temp_db, project_id="project-1")

    assert result.enabled is True
    assert CronJobStorage(temp_db).get_job_by_name(DISPATCHER_CRON_JOB_NAME).enabled is True


def test_lifecycle_event_appended(temp_db) -> None:
    from gobby.build.service import build_stop

    result = build_stop(db=temp_db, project_id="project-1")

    assert result.lifecycle_event.reason == "gobby build stop"
    row = temp_db.fetchone(
        "SELECT reason FROM project_lifecycle_events WHERE project_id = ?",
        ("project-1",),
    )
    assert row["reason"] == "gobby build stop"


def test_in_flight_agents_unaffected(monkeypatch: pytest.MonkeyPatch, temp_db) -> None:
    killed: list[str] = []
    monkeypatch.setattr(
        "gobby.build.service.kill_agent", lambda run_id: killed.append(run_id), raising=False
    )

    from gobby.build.service import build_stop

    build_stop(db=temp_db, project_id="project-1")

    assert killed == []


@pytest.mark.asyncio
async def test_kick_no_op_when_dispatcher_disabled() -> None:
    from gobby.build.lifecycle import _kick_dispatcher_tick

    summary = await _kick_dispatcher_tick(dispatcher_enabled=False)

    assert summary.ticks == 0
    assert summary.reason == "dispatcher_cron_disabled"


@pytest.mark.asyncio
async def test_kick_fires_when_dispatcher_enabled() -> None:
    from gobby.build.lifecycle import _kick_dispatcher_tick

    summary = await _kick_dispatcher_tick(dispatcher_enabled=True)

    assert summary.ticks == 0
    assert summary.reason == "database_missing"


def test_no_task_flag_exposed() -> None:
    from gobby.cli.build import build_stop_command

    param_names = {param.name for param in build_stop_command.params}
    assert "task" not in param_names
