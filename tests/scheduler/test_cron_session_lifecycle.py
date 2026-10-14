"""Cron parent cleanup follows durable work, not session age or job enablement."""

from uuid import uuid4

import pytest

from gobby.scheduler.executor import CronExecutor
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob, CronRun
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager, system_session_id

pytestmark = pytest.mark.integration
PROJECT_ID = "00000000-0000-0000-0000-000000000000"


def _parent(db: HubDatabase, status: str) -> tuple[CronJob, CronRun, str]:
    storage = CronJobStorage(db)
    job = storage.create_job(
        project_id=PROJECT_ID,
        name=f"status-report-{uuid4()}",
        schedule_type="cron",
        cron_expr="0 * * * *",
        action_type="pipeline",
        action_config={"pipeline_name": "status-report"},
        enabled=False,
    )
    run = storage.create_run(job.id)
    assert run is not None
    storage.update_run(run.id, status=status)
    parent = SessionManager(db).register(
        external_id=f"cron-{job.id}-{run.id}-status-report",
        machine_id=None,
        project_id=PROJECT_ID,
        source="cron",
        title="cron:status-report",
        parent_session_id=system_session_id(),
    )
    return job, run, parent.id


@pytest.mark.parametrize("status", ["completed", "failed", "skipped"])
def test_terminal_cron_parent_is_closed_even_for_disabled_job(
    temp_db: HubDatabase, status: str
) -> None:
    job, run, parent_id = _parent(temp_db, status)
    storage = CronJobStorage(temp_db)
    executor = CronExecutor(storage=storage)

    assert executor.reap_finished_cron_sessions(run.machine_id) == 1
    parent = SessionManager(temp_db).get(parent_id)
    assert parent is not None and parent.status == "deleted"
    persisted_job = storage.get_job(job.id)
    assert persisted_job is not None and not persisted_job.enabled
    assert executor.reap_finished_cron_sessions(run.machine_id) == 0


@pytest.mark.parametrize(
    "status,closed",
    [
        ("completed", True),
        ("failed", True),
        ("cancelled", True),
        ("running", False),
        ("waiting_approval", False),
        ("interrupted", False),
    ],
)
def test_dispatched_pipeline_parent_follows_child_status(
    temp_db: HubDatabase, status: str, closed: bool
) -> None:
    _, run, parent_id = _parent(temp_db, "dispatched")
    execution_id = str(uuid4())
    temp_db.execute(
        "INSERT INTO pipeline_executions (id, pipeline_name, project_id, status) "
        "VALUES (%s, %s, %s, %s)",
        (execution_id, "status-report", PROJECT_ID, status),
    )
    storage = CronJobStorage(temp_db)
    storage.update_run(run.id, pipeline_execution_id=execution_id)

    assert CronExecutor(storage=storage).reap_finished_cron_sessions(run.machine_id) == int(closed)
    parent = SessionManager(temp_db).get(parent_id)
    assert parent is not None
    assert (parent.status == "deleted") is closed


@pytest.mark.parametrize("terminal", [False, True])
def test_dispatched_agent_parent_follows_child_status(temp_db: HubDatabase, terminal: bool) -> None:
    _, run, parent_id = _parent(temp_db, "dispatched")
    manager = LocalAgentRunManager(temp_db)
    agent = manager.create(parent_session_id=parent_id, provider="claude", prompt="isolated test")
    if terminal:
        manager.complete(agent.id, result="done")
    storage = CronJobStorage(temp_db)
    storage.update_run(run.id, agent_run_id=agent.id)

    assert CronExecutor(storage=storage).reap_finished_cron_sessions(run.machine_id) == int(
        terminal
    )
    parent = SessionManager(temp_db).get(parent_id)
    assert parent is not None
    assert (parent.status == "deleted") is terminal


def test_active_unlinked_and_foreign_machine_parents_are_retained(temp_db: HubDatabase) -> None:
    _, pending, pending_id = _parent(temp_db, "pending")
    _, dispatched, dispatched_id = _parent(temp_db, "dispatched")
    _, terminal, terminal_id = _parent(temp_db, "completed")
    executor = CronExecutor(storage=CronJobStorage(temp_db))

    assert executor.reap_finished_cron_sessions(str(uuid4())) == 0
    assert executor.reap_finished_cron_sessions(terminal.machine_id) == 1
    sessions = SessionManager(temp_db)
    for parent_id in (pending_id, dispatched_id):
        parent = sessions.get(parent_id)
        assert parent is not None and parent.status != "deleted"
    parent = sessions.get(terminal_id)
    assert parent is not None and parent.status == "deleted"
    assert pending.machine_id == dispatched.machine_id


def test_missing_linked_child_retains_cron_parent(temp_db: HubDatabase) -> None:
    _, run, parent_id = _parent(temp_db, "dispatched")
    storage = CronJobStorage(temp_db)
    storage.update_run(run.id, pipeline_execution_id=str(uuid4()))

    assert CronExecutor(storage=storage).reap_finished_cron_sessions(run.machine_id) == 0
    parent = SessionManager(temp_db).get(parent_id)
    assert parent is not None and parent.status != "deleted"
