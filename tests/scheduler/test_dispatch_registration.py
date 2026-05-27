"""Tests for removed dispatcher cron registration."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _seed_project(temp_db) -> None:
    temp_db.execute(
        """
        INSERT INTO projects (id, name, created_at, updated_at)
        VALUES ('project-1', 'Project 1', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT (id) DO NOTHING
        """
    )


def test_legacy_automation_cron_rows_are_deleted(temp_db) -> None:
    from gobby.storage.cron import CronJobStorage
    from gobby.system_automation import remove_legacy_automation_cron_rows

    _seed_project(temp_db)
    storage = CronJobStorage(temp_db)
    dispatcher = storage.create_job(
        project_id="project-1",
        name="gobby:dispatcher",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "dispatch.tick"},
        interval_seconds=60,
        is_system=True,
    )
    heartbeat = storage.create_job(
        project_id="project-1",
        name="gobby:pipeline-heartbeat",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "pipeline_heartbeat"},
        interval_seconds=60,
        is_system=True,
    )
    storage.create_run(dispatcher.id)
    storage.create_run(heartbeat.id)

    removed = remove_legacy_automation_cron_rows(temp_db)

    assert removed == 2
    assert storage.get_job_by_name("gobby:dispatcher") is None
    assert storage.get_job_by_name("gobby:pipeline-heartbeat") is None
    rows = temp_db.fetchall("SELECT * FROM cron_runs")
    assert rows == []


def test_legacy_automation_cron_cleanup_keeps_user_jobs(temp_db) -> None:
    from gobby.storage.cron import CronJobStorage
    from gobby.system_automation import remove_legacy_automation_cron_rows

    _seed_project(temp_db)
    storage = CronJobStorage(temp_db)
    user_job = storage.create_job(
        project_id="project-1",
        name="user-daily",
        schedule_type="cron",
        cron_expr="0 7 * * *",
        action_type="shell",
        action_config={"command": "echo", "args": ["ok"]},
    )

    removed = remove_legacy_automation_cron_rows(temp_db)

    assert removed == 0
    assert storage.get_job(user_job.id) is not None
