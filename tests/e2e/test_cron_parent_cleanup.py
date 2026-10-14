"""An isolated daemon retires finished cron parents without launching their job."""

from pathlib import Path
from uuid import uuid4

import pytest

from gobby.storage.cron import CronJobStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager, ensure_system_session, system_session_id
from tests._timing import wait_for_condition
from tests.e2e.conftest import DaemonInstance

pytestmark = pytest.mark.e2e
PROJECT_ID = "00000000-0000-0000-0000-000000000e2e"
MACHINE_ID = "21000000-0000-4000-8000-000000000002"


@pytest.fixture
def e2e_pre_daemon_setup(
    e2e_config: tuple[Path, int, int], postgres_db: HubDatabase
) -> tuple[str, str, str]:
    """Seed only the fixture's migrated schema, before the temporary daemon starts."""
    assert e2e_config[0].parent.name == ".gobby-home"
    ensure_system_session(postgres_db)
    job = CronJobStorage(postgres_db).create_job(
        project_id=PROJECT_ID,
        name="isolated-status-report",
        schedule_type="cron",
        cron_expr="0 * * * *",
        action_type="pipeline",
        action_config={"pipeline_name": "isolated-status-report"},
        enabled=False,
    )
    parent_ids: list[str] = []
    for status in ("completed", "dispatched"):
        run_id, parent_id = str(uuid4()), str(uuid4())
        execution_id = str(uuid4()) if status == "dispatched" else None
        if execution_id:
            postgres_db.execute(
                "INSERT INTO pipeline_executions (id, pipeline_name, project_id, status) "
                "VALUES (%s, %s, %s, 'waiting_approval')",
                (execution_id, "isolated-status-report", PROJECT_ID),
            )
        postgres_db.execute(
            "INSERT INTO cron_runs (id, cron_job_id, machine_id, status, "
            "pipeline_execution_id, triggered_at, created_at) "
            "VALUES (%s, %s, %s, %s, %s, NOW(), NOW())",
            (run_id, job.id, MACHINE_ID, status, execution_id),
        )
        postgres_db.execute(
            "INSERT INTO sessions (id, external_id, machine_id, project_id, source, "
            "status, title, parent_session_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, 'cron', 'active', 'cron:isolated-status-report', "
            "%s, NOW(), NOW())",
            (
                parent_id,
                f"cron-{job.id}-{run_id}-isolated-status-report",
                MACHINE_ID,
                PROJECT_ID,
                system_session_id(),
            ),
        )
        parent_ids.append(parent_id)
    return job.id, parent_ids[0], parent_ids[1]


def test_daemon_reaps_finished_parent_and_retains_approval_parent(
    daemon_instance: DaemonInstance,
    e2e_pre_daemon_setup: tuple[str, str, str],
    postgres_db: HubDatabase,
) -> None:
    job_id, finished_id, approval_id = e2e_pre_daemon_setup
    sessions = SessionManager(postgres_db)

    def finished_parent_deleted() -> bool:
        parent = sessions.get(finished_id)
        return parent is not None and parent.status == "deleted"

    assert wait_for_condition(finished_parent_deleted, timeout=30), daemon_instance.read_logs()
    approval_parent = sessions.get(approval_id)
    assert approval_parent is not None and approval_parent.status == "active"
    storage = CronJobStorage(postgres_db)
    job = storage.get_job(job_id)
    assert job is not None and not job.enabled
    assert len(storage.list_runs(job_id)) == 2
    assert daemon_instance.is_alive()
