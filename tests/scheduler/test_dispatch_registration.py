"""Red tests for installing the bundled dispatcher cron row."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_dispatcher_row_marked_system(temp_db) -> None:
    from gobby.runner import install_dispatcher_cron_row
    from gobby.storage.cron import CronJobStorage

    job = install_dispatcher_cron_row(temp_db, project_id="project-1")

    assert CronJobStorage(temp_db).get_job(job.id).is_system is True


def test_existing_enabled_preserved_on_upgrade(temp_db) -> None:
    from gobby.runner import install_dispatcher_cron_row
    from gobby.storage.cron import CronJobStorage

    first = install_dispatcher_cron_row(temp_db, project_id="project-1")
    CronJobStorage(temp_db).update_job(first.id, enabled=False)
    upgraded = install_dispatcher_cron_row(temp_db, project_id="project-1")

    assert upgraded.enabled is False


def test_first_install_seeds_action_and_schedule_defaults(temp_db) -> None:
    from gobby.runner import install_dispatcher_cron_row

    job = install_dispatcher_cron_row(temp_db, project_id="project-1")

    assert job.action_type == "handler"
    assert job.action_config == {"handler": "dispatch.tick"}
    assert job.schedule_type == "interval"
    assert job.interval_seconds == 60


def test_upgrade_reconciles_action_and_schedule(temp_db) -> None:
    from gobby.runner import install_dispatcher_cron_row
    from gobby.storage.cron import CronJobStorage

    first = install_dispatcher_cron_row(temp_db, project_id="project-1")
    CronJobStorage(temp_db).update_job(first.id, interval_seconds=300)
    CronJobStorage(temp_db).update_system_job_bookkeeping(first.id, last_status="completed")
    upgraded = install_dispatcher_cron_row(temp_db, project_id="project-1")

    assert upgraded.action_config == {"handler": "dispatch.tick"}
    assert upgraded.schedule_type == "interval"
    assert upgraded.interval_seconds == 60
    assert upgraded.last_status == "completed"


def test_existing_disabled_state_survives_schedule_repair(temp_db) -> None:
    from gobby.runner import install_dispatcher_cron_row
    from gobby.storage.cron import CronJobStorage

    first = install_dispatcher_cron_row(temp_db, project_id="project-1")
    CronJobStorage(temp_db).update_job(first.id, enabled=False, interval_seconds=300)
    upgraded = install_dispatcher_cron_row(temp_db, project_id="project-1")

    assert upgraded.enabled is False
    assert upgraded.interval_seconds == 60


def test_action_config_repaired_with_operator_schedule_intact(temp_db) -> None:
    from gobby.runner import install_dispatcher_cron_row
    from gobby.storage.cron import CronJobStorage

    first = install_dispatcher_cron_row(temp_db, project_id="project-1")
    storage = CronJobStorage(temp_db)
    storage.update_job(first.id, interval_seconds=300)
    temp_db.execute(
        "UPDATE cron_jobs SET action_config = %s WHERE id = %s",
        ('{"handler": "drifted"}', first.id),
    )
    upgraded = install_dispatcher_cron_row(temp_db, project_id="project-1")

    assert upgraded.action_config == {"handler": "dispatch.tick"}
    assert upgraded.interval_seconds == 60
