"""Tests for cron job storage CRUD and compute_next_run."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from gobby.storage.cron import CronJobStorage, compute_next_run
from gobby.storage.cron_models import CronJob

if TYPE_CHECKING:
    from gobby.storage.database import LocalDatabase

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def cron_storage(temp_db: LocalDatabase) -> CronJobStorage:
    """Create a CronJobStorage with the temp database."""
    return CronJobStorage(temp_db)


# --- Migration tests (#7620) ---


def test_cron_jobs_table_exists(temp_db: LocalDatabase) -> None:
    """Migration creates cron_jobs table."""
    row = temp_db.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='cron_jobs'")
    assert row is not None


def test_cron_runs_table_exists(temp_db: LocalDatabase) -> None:
    """Migration creates cron_runs table."""
    row = temp_db.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='cron_runs'")
    assert row is not None


def test_cron_jobs_has_expected_columns(temp_db: LocalDatabase) -> None:
    """cron_jobs table has all required columns."""
    columns = {row["name"] for row in temp_db.fetchall("PRAGMA table_info(cron_jobs)")}
    expected = {
        "id",
        "project_id",
        "name",
        "description",
        "schedule_type",
        "cron_expr",
        "interval_seconds",
        "run_at",
        "timezone",
        "action_type",
        "action_config",
        "enabled",
        "next_run_at",
        "last_run_at",
        "last_status",
        "consecutive_failures",
        "created_at",
        "updated_at",
    }
    assert expected.issubset(columns)


# --- CronJobStorage CRUD tests (#7621) ---


def test_create_job(cron_storage: CronJobStorage) -> None:
    """create_job inserts and returns a CronJob."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Test Job",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo", "args": ["hello"]},
        cron_expr="0 7 * * *",
    )
    assert job.id.startswith("cj-")
    assert job.name == "Test Job"
    assert job.schedule_type == "cron"
    assert job.action_type == "shell"
    assert job.enabled is True


def test_get_job(cron_storage: CronJobStorage) -> None:
    """get_job retrieves by ID."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Get Test",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=60,
    )
    retrieved = cron_storage.get_job(job.id)
    assert retrieved is not None
    assert retrieved.name == "Get Test"
    assert retrieved.interval_seconds == 60


def test_get_job_not_found(cron_storage: CronJobStorage) -> None:
    """get_job returns None for non-existent ID."""
    assert cron_storage.get_job("cj-nonexistent") is None


def test_list_jobs(cron_storage: CronJobStorage) -> None:
    """list_jobs returns all jobs for a project."""
    cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Job 1",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Job 2",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=300,
    )
    jobs = cron_storage.list_jobs(project_id=PROJECT_ID)
    assert len(jobs) == 2


def test_list_jobs_enabled_filter(cron_storage: CronJobStorage) -> None:
    """list_jobs filters by enabled state."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Enabled",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Disabled",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
        enabled=False,
    )
    enabled_jobs = cron_storage.list_jobs(project_id=PROJECT_ID, enabled=True)
    assert len(enabled_jobs) == 1
    assert enabled_jobs[0].id == job.id


def test_update_job(cron_storage: CronJobStorage) -> None:
    """update_job modifies specified fields."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Original",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    updated = cron_storage.update_job(job.id, name="Updated", description="new desc")
    assert updated is not None
    assert updated.name == "Updated"
    assert updated.description == "new desc"


def test_update_job_invalid_field(cron_storage: CronJobStorage) -> None:
    """update_job rejects invalid field names."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Test",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    with pytest.raises(ValueError, match="Invalid field names"):
        cron_storage.update_job(job.id, fake_field="bad")


def test_delete_job(cron_storage: CronJobStorage) -> None:
    """delete_job removes a job and its runs."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="To Delete",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    # Create a run for the job
    cron_storage.create_run(job.id)
    assert cron_storage.delete_job(job.id) is True
    assert cron_storage.get_job(job.id) is None
    assert cron_storage.list_runs(job.id) == []


def test_toggle_job(cron_storage: CronJobStorage) -> None:
    """toggle_job flips enabled state."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Toggle Me",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    assert job.enabled is True
    toggled = cron_storage.toggle_job(job.id)
    assert toggled is not None
    assert toggled.enabled is False
    assert toggled.next_run_at is None
    # Toggle back
    toggled2 = cron_storage.toggle_job(job.id)
    assert toggled2 is not None
    assert toggled2.enabled is True
    assert toggled2.next_run_at is not None


def test_get_due_jobs(cron_storage: CronJobStorage) -> None:
    """get_due_jobs returns jobs whose next_run_at has passed."""
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    job1 = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Due",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    cron_storage.update_job(job1.id, next_run_at=past)

    job2 = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Not Due",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    cron_storage.update_job(job2.id, next_run_at=future)

    due = cron_storage.get_due_jobs()
    assert len(due) == 1
    assert due[0].id == job1.id


# --- CronRun CRUD tests (#7621) ---


def test_create_run(cron_storage: CronJobStorage) -> None:
    """create_run inserts and returns a CronRun."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Run Test",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    run = cron_storage.create_run(job.id)
    assert run.id.startswith("cr-")
    assert run.cron_job_id == job.id
    assert run.status == "pending"


def test_update_run(cron_storage: CronJobStorage) -> None:
    """update_run changes status and output."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Update Run",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    run = cron_storage.create_run(job.id)
    now = datetime.now(UTC).isoformat()
    updated = cron_storage.update_run(
        run.id,
        status="completed",
        started_at=now,
        completed_at=now,
        output="hello world",
    )
    assert updated is not None
    assert updated.status == "completed"
    assert updated.output == "hello world"


def test_list_runs(cron_storage: CronJobStorage) -> None:
    """list_runs returns runs for a job."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="List Runs",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    cron_storage.create_run(job.id)
    cron_storage.create_run(job.id)
    runs = cron_storage.list_runs(job.id)
    assert len(runs) == 2


def test_count_running(cron_storage: CronJobStorage) -> None:
    """count_running returns number of running jobs."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Count Test",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    run = cron_storage.create_run(job.id)
    assert cron_storage.count_running() == 0
    cron_storage.update_run(run.id, status="running")
    assert cron_storage.count_running() == 1


def test_cleanup_old_runs(cron_storage: CronJobStorage) -> None:
    """cleanup_old_runs deletes runs older than threshold."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Cleanup",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    # Create a recent run
    cron_storage.create_run(job.id)
    # Simulate old run by manually inserting
    old_time = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    cron_storage.db.execute(
        """INSERT INTO cron_runs (id, cron_job_id, triggered_at, status, created_at)
        VALUES (?, ?, ?, 'completed', ?)""",
        ("cr-old", job.id, old_time, old_time),
    )
    assert len(cron_storage.list_runs(job.id)) == 2
    deleted = cron_storage.cleanup_old_runs(30)
    assert deleted == 1
    assert len(cron_storage.list_runs(job.id)) == 1


# --- compute_next_run tests (#7622) ---


def test_compute_next_run_cron() -> None:
    """compute_next_run with cron expression returns correct datetime."""
    job = CronJob(
        id="cj-1",
        project_id="p",
        name="test",
        schedule_type="cron",
        action_type="shell",
        action_config={},
        created_at="",
        updated_at="",
        cron_expr="0 7 * * *",
        timezone="UTC",
        enabled=True,
    )
    next_run = compute_next_run(job)
    assert next_run is not None
    assert next_run.hour == 7


def test_compute_next_run_interval_no_last_run() -> None:
    """compute_next_run with interval and no last run uses now + interval."""
    job = CronJob(
        id="cj-1",
        project_id="p",
        name="test",
        schedule_type="interval",
        action_type="shell",
        action_config={},
        created_at="",
        updated_at="",
        interval_seconds=300,
        timezone="UTC",
        enabled=True,
    )
    next_run = compute_next_run(job)
    assert next_run is not None
    # Should be roughly 5 minutes from now
    diff = next_run - datetime.now(UTC)
    assert 290 < diff.total_seconds() < 310


def test_compute_next_run_interval_with_last_run() -> None:
    """compute_next_run with interval adds timedelta from last_run_at."""
    last = datetime.now(UTC).isoformat()
    job = CronJob(
        id="cj-1",
        project_id="p",
        name="test",
        schedule_type="interval",
        action_type="shell",
        action_config={},
        created_at="",
        updated_at="",
        interval_seconds=60,
        timezone="UTC",
        enabled=True,
        last_run_at=last,
    )
    next_run = compute_next_run(job)
    assert next_run is not None
    diff = next_run - datetime.now(UTC)
    assert 50 < diff.total_seconds() < 70


def test_compute_next_run_once_future() -> None:
    """compute_next_run with 'once' schedule uses run_at for future time."""
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    job = CronJob(
        id="cj-1",
        project_id="p",
        name="test",
        schedule_type="once",
        action_type="shell",
        action_config={},
        created_at="",
        updated_at="",
        run_at=future,
        timezone="UTC",
        enabled=True,
    )
    next_run = compute_next_run(job)
    assert next_run is not None


def test_compute_next_run_once_expired() -> None:
    """compute_next_run returns None for expired one-shot."""
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    job = CronJob(
        id="cj-1",
        project_id="p",
        name="test",
        schedule_type="once",
        action_type="shell",
        action_config={},
        created_at="",
        updated_at="",
        run_at=past,
        timezone="UTC",
        enabled=True,
    )
    next_run = compute_next_run(job)
    assert next_run is None


def test_compute_next_run_disabled() -> None:
    """compute_next_run returns None for disabled jobs."""
    job = CronJob(
        id="cj-1",
        project_id="p",
        name="test",
        schedule_type="cron",
        action_type="shell",
        action_config={},
        created_at="",
        updated_at="",
        cron_expr="0 7 * * *",
        timezone="UTC",
        enabled=False,
    )
    next_run = compute_next_run(job)
    assert next_run is None


def test_compute_next_run_respects_timezone() -> None:
    """compute_next_run respects timezone setting."""
    job = CronJob(
        id="cj-1",
        project_id="p",
        name="test",
        schedule_type="cron",
        action_type="shell",
        action_config={},
        created_at="",
        updated_at="",
        cron_expr="0 7 * * *",
        timezone="America/Los_Angeles",
        enabled=True,
    )
    next_run = compute_next_run(job)
    assert next_run is not None
    # Result should be in UTC
    assert next_run.tzinfo is not None
