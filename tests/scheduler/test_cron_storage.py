"""Tests for cron job storage CRUD and compute_next_run."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from gobby.storage.cron import CronJobStorage, compute_next_run
from gobby.storage.cron_children import _fetch_statuses
from gobby.storage.cron_models import CronJob

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

PROJECT_ID = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def cron_storage(temp_db: HubDatabase) -> CronJobStorage:
    """Create a CronJobStorage with the temp database."""
    return CronJobStorage(temp_db)


# --- Migration tests (#7620) ---


def test_cron_jobs_table_exists(temp_db: HubDatabase) -> None:
    """Migration creates cron_jobs table."""
    row = temp_db.fetchone(
        "SELECT table_name FROM information_schema.tables WHERE table_name = %s",
        ("cron_jobs",),
    )
    assert row is not None


def test_cron_runs_table_exists(temp_db: HubDatabase) -> None:
    """Migration creates cron_runs table."""
    row = temp_db.fetchone(
        "SELECT table_name FROM information_schema.tables WHERE table_name = %s",
        ("cron_runs",),
    )
    assert row is not None


def test_fetch_statuses_rejects_unknown_child_table(temp_db: HubDatabase) -> None:
    with pytest.raises(ValueError, match="unsupported cron child status table"):
        _fetch_statuses(temp_db, "cron_runs; DROP TABLE cron_runs", ["cr-1"])


def test_cron_jobs_has_expected_columns(temp_db: HubDatabase) -> None:
    """cron_jobs table has all required columns."""
    columns = {
        row["column_name"]
        for row in temp_db.fetchall(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            ("cron_jobs",),
        )
    }
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
    assert str(uuid.UUID(job.id)) == job.id
    assert job.name == "Test Job"
    assert job.schedule_type == "cron"
    assert job.action_type == "shell"
    assert job.enabled is True


def test_create_interval_job_clamps_to_minimum_interval(
    cron_storage: CronJobStorage,
) -> None:
    """Interval cron jobs cannot run more often than once per minute."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Too Fast",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=10,
    )

    assert job.interval_seconds == 60


def test_update_interval_job_clamps_to_minimum_interval(
    cron_storage: CronJobStorage,
) -> None:
    """Updating an interval cron job also enforces the one-minute floor."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Update Too Fast",
        schedule_type="interval",
        action_type="shell",
        action_config={"command": "echo"},
        interval_seconds=300,
    )
    updated = cron_storage.update_job(job.id, interval_seconds=10)

    assert updated is not None
    assert updated.interval_seconds == 60


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
    assert cron_storage.get_job("00000000-0000-0000-0000-0000000000ff") is None


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


def test_list_system_jobs_by_name_prefix(cron_storage: CronJobStorage) -> None:
    """list_system_jobs_by_name_prefix returns system rows matching the prefix."""

    def _create(name: str, *, enabled: bool, is_system: bool) -> CronJob:
        return cron_storage.create_job(
            project_id=PROJECT_ID,
            name=name,
            schedule_type="interval",
            action_type="handler",
            action_config={"handler": name},
            interval_seconds=300,
            enabled=enabled,
            is_system=is_system,
        )

    matching_enabled = _create("gobby:wiki-refresh:project:a", enabled=True, is_system=True)
    matching_disabled = _create("gobby:wiki-health:project:a", enabled=False, is_system=True)
    _create("gobby:wiki-audit:project:b", enabled=True, is_system=False)
    _create("gobby:other-job", enabled=True, is_system=True)

    all_system = cron_storage.list_system_jobs_by_name_prefix("gobby:wiki-")
    assert {job.id for job in all_system} == {matching_enabled.id, matching_disabled.id}

    enabled_only = cron_storage.list_system_jobs_by_name_prefix("gobby:wiki-", enabled=True)
    assert [job.id for job in enabled_only] == [matching_enabled.id]

    with pytest.raises(ValueError, match="prefix must not be empty"):
        cron_storage.list_system_jobs_by_name_prefix("")


def test_list_system_jobs_by_name_prefix_escapes_like_wildcards(
    cron_storage: CronJobStorage,
) -> None:
    """LIKE metacharacters in the prefix match literally, not as wildcards."""
    underscore = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="gobby:wiki_special",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "gobby:wiki_special"},
        interval_seconds=300,
        enabled=True,
        is_system=True,
    )
    cron_storage.create_job(
        project_id=PROJECT_ID,
        name="gobby:wikiXspecial",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "gobby:wikiXspecial"},
        interval_seconds=300,
        enabled=True,
        is_system=True,
    )

    matches = cron_storage.list_system_jobs_by_name_prefix("gobby:wiki_")
    assert [job.id for job in matches] == [underscore.id]


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


def test_park_system_job_clears_next_run_without_disabling(
    cron_storage: CronJobStorage,
) -> None:
    """Parking a system job clears next_run_at but preserves enabled=true."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="gobby:dispatcher",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "dispatch.tick"},
        interval_seconds=60,
        enabled=True,
        is_system=True,
    )
    assert job.next_run_at is not None

    parked = cron_storage.park_system_job(job.id)

    assert parked is not None
    assert parked.enabled is True
    assert parked.next_run_at is None


def test_wake_system_job_recomputes_next_run_for_enabled_row(
    cron_storage: CronJobStorage,
) -> None:
    """Waking a parked enabled system job schedules its next run."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="gobby:dispatcher",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "dispatch.tick"},
        interval_seconds=60,
        enabled=True,
        is_system=True,
    )
    cron_storage.park_system_job(job.id)

    woken = cron_storage.wake_system_job(job.id)

    assert woken is not None
    assert woken.enabled is True
    assert woken.next_run_at is not None


def test_wake_system_job_leaves_disabled_hard_stop_parked(
    cron_storage: CronJobStorage,
) -> None:
    """Disabled system rows represent hard stops and do not wake."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="gobby:dispatcher",
        schedule_type="interval",
        action_type="handler",
        action_config={"handler": "dispatch.tick"},
        interval_seconds=60,
        enabled=False,
        is_system=True,
    )

    woken = cron_storage.wake_system_job(job.id)

    assert woken is not None
    assert woken.enabled is False
    assert woken.next_run_at is None


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
    assert run is not None
    assert str(uuid.UUID(run.id)) == run.id
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
    assert run is not None
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
    first = cron_storage.create_run(job.id)
    assert first is not None
    cron_storage.update_run(first.id, status="completed")
    second = cron_storage.create_run(job.id)
    assert second is not None
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
    assert run is not None
    assert cron_storage.count_running() == 1
    cron_storage.update_run(run.id, status="running")
    assert cron_storage.count_running() == 1


def test_create_run_returns_none_when_job_already_active(
    cron_storage: CronJobStorage,
) -> None:
    """create_run atomically rejects a second pending/running row for one job."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Atomic Active Guard",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )

    first = cron_storage.create_run(job.id)
    second = cron_storage.create_run(job.id)
    assert first is not None
    assert second is None

    cron_storage.update_run(first.id, status="completed")
    third = cron_storage.create_run(job.id)
    assert third is not None


def test_list_active_runs_returns_only_pending_and_running(
    cron_storage: CronJobStorage,
) -> None:
    """list_active_runs surfaces pending/running rows across jobs, skipping terminal ones."""
    pending_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Pending Job",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    pending = cron_storage.create_run(pending_job.id)
    assert pending is not None
    running_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Running Job",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    running = cron_storage.create_run(running_job.id)
    assert running is not None
    cron_storage.update_run(running.id, status="running")
    completed_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Completed Job",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    completed = cron_storage.create_run(completed_job.id)
    assert completed is not None
    cron_storage.update_run(completed.id, status="completed")

    active = cron_storage.list_active_runs()

    assert {run.id for run in active} == {pending.id, running.id}


def test_fail_run_if_active_transitions_only_active_rows(
    cron_storage: CronJobStorage,
) -> None:
    """fail_run_if_active fails a pending/running row and no-ops on terminal rows."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Fail If Active Test",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    running = cron_storage.create_run(job.id)
    assert running is not None
    cron_storage.update_run(running.id, status="running")

    assert cron_storage.fail_run_if_active(running.id, "orphaned") is True
    refreshed = cron_storage.get_run(running.id)
    assert refreshed is not None
    assert refreshed.status == "failed"
    assert refreshed.error == "orphaned"
    assert refreshed.completed_at is not None

    completed = cron_storage.create_run(job.id)
    assert completed is not None
    cron_storage.update_run(completed.id, status="completed")

    assert cron_storage.fail_run_if_active(completed.id, "orphaned") is False
    refreshed_completed = cron_storage.get_run(completed.id)
    assert refreshed_completed is not None
    assert refreshed_completed.status == "completed"
    assert cron_storage.fail_run_if_active("00000000-0000-0000-0000-0000000000ff", "gone") is False


def test_has_running_run_is_scoped_to_job(cron_storage: CronJobStorage) -> None:
    """has_running_run only reports active runs for the requested job."""
    active_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Active Job",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    idle_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Idle Job",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )

    run = cron_storage.create_run(active_job.id)
    assert run is not None
    cron_storage.update_run(run.id, status="running")

    assert cron_storage.has_running_run(active_job.id) is True
    assert cron_storage.has_running_run(idle_job.id) is False


def test_list_runs_hydrates_pipeline_child(cron_storage: CronJobStorage) -> None:
    """Cron run history includes child status projection."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Pipeline Child",
        schedule_type="cron",
        action_type="pipeline",
        action_config={"pipeline_name": "approval"},
        cron_expr="0 * * * *",
    )
    run = cron_storage.create_run(job.id)
    assert run is not None
    cron_storage.db.execute(
        """
        INSERT INTO pipeline_executions (id, pipeline_name, project_id, status)
        VALUES (%s, %s, %s, %s)
        """,
        ("eeeeeeee-eeee-4eee-8eee-eeeeeeee0101", "approval", PROJECT_ID, "waiting_approval"),
    )
    cron_storage.update_run(
        run.id,
        status="dispatched",
        pipeline_execution_id="eeeeeeee-eeee-4eee-8eee-eeeeeeee0101",
        completed_at=datetime.now(UTC).isoformat(),
    )

    runs = cron_storage.list_runs(job.id)

    assert runs[0].child is not None
    assert runs[0].child.to_dict() == {
        "type": "pipeline_execution",
        "id": "eeeeeeee-eeee-4eee-8eee-eeeeeeee0101",
        "status": "waiting_approval",
        "terminal": False,
        "missing": False,
    }


def test_get_run_marks_missing_child(cron_storage: CronJobStorage) -> None:
    """Missing linked child rows are explicit in projection."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Missing Child",
        schedule_type="cron",
        action_type="agent_spawn",
        action_config={"prompt": "hello"},
        cron_expr="0 * * * *",
    )
    run = cron_storage.create_run(job.id)
    assert run is not None
    cron_storage.update_run(
        run.id,
        status="dispatched",
        agent_run_id="eeeeeeee-eeee-4eee-8eee-eeeeeeee0106",
        completed_at=datetime.now(UTC).isoformat(),
    )

    refreshed = cron_storage.get_run(run.id)

    assert refreshed is not None
    assert refreshed.child is not None
    assert refreshed.child.to_dict() == {
        "type": "agent_run",
        "id": "eeeeeeee-eeee-4eee-8eee-eeeeeeee0106",
        "status": None,
        "terminal": False,
        "missing": True,
    }


def test_active_children_for_job_uses_application_statuses(
    cron_storage: CronJobStorage,
) -> None:
    """Active child lookup only reports active dispatched children."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Active Pipeline Child",
        schedule_type="cron",
        action_type="pipeline",
        action_config={"pipeline_name": "approval"},
        cron_expr="0 * * * *",
    )
    run = cron_storage.create_run(job.id)
    assert run is not None
    cron_storage.db.execute(
        """
        INSERT INTO pipeline_executions (id, pipeline_name, project_id, status)
        VALUES (%s, %s, %s, %s)
        """,
        ("eeeeeeee-eeee-4eee-8eee-eeeeeeee0102", "approval", PROJECT_ID, "interrupted"),
    )
    cron_storage.update_run(
        run.id,
        status="dispatched",
        pipeline_execution_id="eeeeeeee-eeee-4eee-8eee-eeeeeeee0102",
        completed_at=datetime.now(UTC).isoformat(),
    )

    assert cron_storage.active_children_for_job(job.id, "pipeline") == [
        {
            "type": "pipeline_execution",
            "id": "eeeeeeee-eeee-4eee-8eee-eeeeeeee0102",
            "status": "interrupted",
            "terminal": False,
            "missing": False,
        }
    ]


def test_reconcile_interrupted_runs_preserves_active_children(
    cron_storage: CronJobStorage,
) -> None:
    """Startup reconciliation dispatches linked active children and fails stale rows."""
    pipeline_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Recon Pipeline",
        schedule_type="cron",
        action_type="pipeline",
        action_config={"pipeline_name": "approval"},
        cron_expr="0 * * * *",
    )
    pipeline_run = cron_storage.create_run(pipeline_job.id)
    assert pipeline_run is not None
    cron_storage.db.execute(
        """
        INSERT INTO pipeline_executions (id, pipeline_name, project_id, status)
        VALUES (%s, %s, %s, %s)
        """,
        ("eeeeeeee-eeee-4eee-8eee-eeeeeeee0103", "approval", PROJECT_ID, "running"),
    )
    cron_storage.update_run(
        pipeline_run.id,
        status="running",
        pipeline_execution_id="eeeeeeee-eeee-4eee-8eee-eeeeeeee0103",
    )
    stale_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Recon Stale",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    stale_run = cron_storage.create_run(stale_job.id)
    assert stale_run is not None

    result = cron_storage.reconcile_interrupted_runs()

    assert result == {"dispatched": 1, "failed": 1}
    refreshed_pipeline = cron_storage.get_run(pipeline_run.id)
    refreshed_stale = cron_storage.get_run(stale_run.id)
    assert refreshed_pipeline is not None
    assert refreshed_pipeline.status == "dispatched"
    assert refreshed_pipeline.child is not None
    assert refreshed_pipeline.child.status == "running"
    assert refreshed_stale is not None
    assert refreshed_stale.status == "failed"


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
    running_job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Cleanup Running",
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
        VALUES (%s, %s, %s, 'completed', %s)""",
        ("eeeeeeee-eeee-4eee-8eee-eeeeeeee0104", job.id, old_time, old_time),
    )
    cron_storage.db.execute(
        """INSERT INTO cron_runs (id, cron_job_id, triggered_at, status, created_at)
        VALUES (%s, %s, %s, 'running', %s)""",
        ("eeeeeeee-eeee-4eee-8eee-eeeeeeee0105", running_job.id, old_time, old_time),
    )
    assert len(cron_storage.list_runs(job.id)) == 2
    deleted = cron_storage.cleanup_old_runs(30)
    assert deleted == 1
    assert len(cron_storage.list_runs(job.id)) == 1
    assert cron_storage.get_run("eeeeeeee-eeee-4eee-8eee-eeeeeeee0105") is not None


@pytest.mark.parametrize("days", [0, -1, True, 1.5, "7"])
def test_cleanup_old_runs_rejects_invalid_days_before_delete(
    cron_storage: CronJobStorage,
    days: object,
) -> None:
    """cleanup_old_runs validates the retention window before deleting rows."""
    job = cron_storage.create_job(
        project_id=PROJECT_ID,
        name="Cleanup invalid days",
        schedule_type="cron",
        action_type="shell",
        action_config={"command": "echo"},
        cron_expr="0 * * * *",
    )
    old_time = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    cron_storage.db.execute(
        """INSERT INTO cron_runs (id, cron_job_id, triggered_at, status, created_at)
        VALUES (%s, %s, %s, 'completed', %s)""",
        (str(uuid.uuid4()), job.id, old_time, old_time),
    )

    with pytest.raises(ValueError, match="days must be a positive integer"):
        cron_storage.cleanup_old_runs(days)

    assert len(cron_storage.list_runs(job.id)) == 1


# --- compute_next_run tests (#7622) ---

CRON_MODEL_TIMESTAMP = datetime(2026, 1, 1, tzinfo=UTC)


def test_compute_next_run_cron() -> None:
    """compute_next_run with cron expression returns correct datetime."""
    job = CronJob(
        id="cj-1",
        project_id="p",
        name="test",
        schedule_type="cron",
        action_type="shell",
        action_config={},
        created_at=CRON_MODEL_TIMESTAMP,
        updated_at=CRON_MODEL_TIMESTAMP,
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
        created_at=CRON_MODEL_TIMESTAMP,
        updated_at=CRON_MODEL_TIMESTAMP,
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
    """compute_next_run with interval always computes from now, not last_run_at."""
    last = datetime.now(UTC).isoformat()
    job = CronJob(
        id="cj-1",
        project_id="p",
        name="test",
        schedule_type="interval",
        action_type="shell",
        action_config={},
        created_at=CRON_MODEL_TIMESTAMP,
        updated_at=CRON_MODEL_TIMESTAMP,
        interval_seconds=60,
        timezone="UTC",
        enabled=True,
        last_run_at=last,
    )
    next_run = compute_next_run(job)
    assert next_run is not None
    diff = next_run - datetime.now(UTC)
    assert 50 < diff.total_seconds() < 70


def test_compute_next_run_interval_stale_last_run_no_double_fire() -> None:
    """Regression: stale last_run_at must not cause next_run in the past (double-fire)."""
    # Simulate: job ran 5 minutes ago with 5-minute interval.
    # Old bug: last_run_at + interval ≈ now → immediate re-fire.
    stale = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    job = CronJob(
        id="cj-1",
        project_id="p",
        name="test",
        schedule_type="interval",
        action_type="handler",
        action_config={},
        created_at=CRON_MODEL_TIMESTAMP,
        updated_at=CRON_MODEL_TIMESTAMP,
        interval_seconds=300,
        timezone="UTC",
        enabled=True,
        last_run_at=stale,
    )
    next_run = compute_next_run(job)
    assert next_run is not None
    # Must be in the future, not ≈ now
    diff = next_run - datetime.now(UTC)
    assert diff.total_seconds() > 290, (
        f"next_run should be ~5min in the future, got {diff.total_seconds():.1f}s"
    )


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
        created_at=CRON_MODEL_TIMESTAMP,
        updated_at=CRON_MODEL_TIMESTAMP,
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
        created_at=CRON_MODEL_TIMESTAMP,
        updated_at=CRON_MODEL_TIMESTAMP,
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
        created_at=CRON_MODEL_TIMESTAMP,
        updated_at=CRON_MODEL_TIMESTAMP,
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
        created_at=CRON_MODEL_TIMESTAMP,
        updated_at=CRON_MODEL_TIMESTAMP,
        cron_expr="0 7 * * *",
        timezone="America/Los_Angeles",
        enabled=True,
    )
    next_run = compute_next_run(job)
    assert next_run is not None
    # Result should be in UTC
    assert next_run.tzinfo is not None
