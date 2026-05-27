"""Red tests for project-wide build stop/resume."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def test_stop_pauses_project_automation_without_cron_row(temp_db) -> None:
    from gobby.build.project_state import is_project_automation_enabled
    from gobby.build.service import build_stop
    from gobby.storage.cron import CronJobStorage

    result = build_stop(db=temp_db, project_id="project-1")

    assert result.enabled is False
    assert is_project_automation_enabled(temp_db, "project-1") is False
    assert CronJobStorage(temp_db).get_job_by_name("gobby:dispatcher") is None


def test_resume_enables_project_automation_without_cron_row(temp_db) -> None:
    from gobby.build.project_state import is_project_automation_enabled
    from gobby.build.service import build_resume, build_stop
    from gobby.storage.cron import CronJobStorage

    build_stop(db=temp_db, project_id="project-1")

    result = build_resume(db=temp_db, project_id="project-1")

    assert result.enabled is True
    assert is_project_automation_enabled(temp_db, "project-1") is True
    assert CronJobStorage(temp_db).get_job_by_name("gobby:dispatcher") is None


def test_stop_creates_project_row_for_control_event(
    temp_db,
) -> None:
    from gobby.build.service import build_stop

    assert temp_db.fetchone("SELECT id FROM projects WHERE id = %s", ("project-1",)) is None

    build_stop(db=temp_db, project_id="project-1")

    assert temp_db.fetchone("SELECT id FROM projects WHERE id = %s", ("project-1",)) is not None


def test_lifecycle_event_appended(temp_db) -> None:
    from gobby.build.service import build_stop

    result = build_stop(db=temp_db, project_id="project-1")

    assert result.lifecycle_event.reason == "gobby build stop"
    row = temp_db.fetchone(
        "SELECT reason FROM project_lifecycle_events WHERE project_id = %s",
        ("project-1",),
    )
    assert row["reason"] == "gobby build stop"


def test_lifecycle_event_id_comes_from_returning_row() -> None:
    from gobby.build.project_controls import _record_project_build_event

    class ReturningCursor:
        lastrowid = None

        def fetchone(self) -> dict[str, int]:
            return {"id": 42}

    class RecordingConnection:
        sql = ""
        params: tuple[Any, ...] = ()

        def execute(self, sql: str, params: tuple[Any, ...]) -> ReturningCursor:
            self.sql = sql
            self.params = params
            return ReturningCursor()

    class ReturningDb:
        conn = RecordingConnection()

        @contextmanager
        def transaction(self) -> Iterator[RecordingConnection]:
            yield self.conn

    db = ReturningDb()

    result = _record_project_build_event(
        db,
        project_id="project-1",
        event="build_resume",
        reason="gobby build resume",
        by_actor="build",
    )

    assert result.id == 42
    assert "RETURNING id" in db.conn.sql
    assert db.conn.params[:4] == (
        "project-1",
        "build_resume",
        "gobby build resume",
        "build",
    )


def test_lifecycle_event_appended_on_hub_database(hub_db) -> None:
    from gobby.build.service import build_resume
    from gobby.storage.projects import LocalProjectManager

    project = LocalProjectManager(hub_db).create(
        name="build-controls-hub",
        repo_path="/tmp/build-controls-hub",
    )

    result = build_resume(db=hub_db, project_id=project.id)

    assert result.lifecycle_event.id > 0
    row = hub_db.fetchone(
        "SELECT reason FROM project_lifecycle_events WHERE project_id = %s",
        (project.id,),
    )
    assert row["reason"] == "gobby build resume"


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
    assert summary.reason == "automation_disabled"


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
