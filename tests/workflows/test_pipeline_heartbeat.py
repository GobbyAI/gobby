"""Tests for PipelineHeartbeat cron handler."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from gobby.scheduler.executor import CronExecutor
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.cron import CronJobStorage
from gobby.storage.cron_models import CronJob, CronRun
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks._manager import LocalTaskManager
from gobby.tasks.state_semantics import projected_task_state
from gobby.workflows.pipeline_heartbeat import PipelineHeartbeat, PipelineHeartbeatResult
from gobby.workflows.pipeline_state import ExecutionStatus

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

PROJECT_ID = "test-project"
SESSION_ID = "sess-test-001"


def _seed_db(db: HubDatabase) -> None:
    """Insert project + session rows to satisfy FK constraints."""
    db.execute(
        """INSERT INTO projects (id, name, repo_path, created_at, updated_at)
           VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
           ON CONFLICT DO NOTHING""",
        (PROJECT_ID, "test-project", "/tmp/test"),
    )
    db.execute(
        """INSERT INTO sessions
           (id, external_id, machine_id, source, project_id, status, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
           ON CONFLICT DO NOTHING""",
        (SESSION_ID, "ext-1", "machine-1", "claude_code", PROJECT_ID, "active"),
    )


@pytest.fixture
def exec_manager(temp_db: HubDatabase) -> LocalPipelineExecutionManager:
    _seed_db(temp_db)
    return LocalPipelineExecutionManager(temp_db, PROJECT_ID)


@pytest.fixture
def heartbeat(
    exec_manager: LocalPipelineExecutionManager,
    agent_run_manager: LocalAgentRunManager,
) -> PipelineHeartbeat:
    return PipelineHeartbeat(
        execution_manager=exec_manager,
        agent_run_manager=agent_run_manager,
        stall_threshold_seconds=60,
    )


def _create_stalled_execution(
    exec_manager: LocalPipelineExecutionManager,
    temp_db: HubDatabase,
    stale_minutes: int = 5,
    session_id: str = SESSION_ID,
) -> str:
    """Create a running execution with an old updated_at timestamp."""
    exe = exec_manager.create_execution(
        pipeline_name="test-pipeline",
        session_id=session_id,
    )
    # Mark as running
    exec_manager.update_execution_status(exe.id, ExecutionStatus.RUNNING)
    # Backdate updated_at to make it stale
    stale_time = (datetime.now(UTC) - timedelta(minutes=stale_minutes)).isoformat()
    temp_db.execute(
        "UPDATE pipeline_executions SET updated_at = %s WHERE id = %s",
        (stale_time, exe.id),
    )
    return exe.id


def _add_alive_agent(
    agent_run_manager: LocalAgentRunManager,
    parent_session_id: str = SESSION_ID,
    run_id: str = "run-alive-001",
) -> None:
    """Create an active agent run in the DB for a parent session."""
    agent_run_manager.create(
        run_id=run_id,
        parent_session_id=parent_session_id,
        provider="test",
        prompt="test agent",
    )
    agent_run_manager.start(run_id)


def _create_pipeline_heartbeat_job(storage: CronJobStorage) -> CronJob:
    return storage.create_job(
        project_id=PROJECT_ID,
        name="gobby:pipeline-heartbeat",
        description="Pipeline heartbeat",
        schedule_type="interval",
        interval_seconds=60,
        action_type="handler",
        action_config={"handler": "pipeline_heartbeat"},
        enabled=True,
        is_system=True,
    )


async def _execute_pipeline_heartbeat_cron(
    storage: CronJobStorage,
    heartbeat: PipelineHeartbeat,
    job: CronJob,
) -> CronRun:
    executor = CronExecutor(storage)
    executor.register_handler("pipeline_heartbeat", heartbeat)
    return await executor.execute(job, storage.create_run(job.id))


@pytest.mark.asyncio
async def test_stalled_no_agents_marks_failed(
    heartbeat: PipelineHeartbeat,
    exec_manager: LocalPipelineExecutionManager,
    temp_db: HubDatabase,
) -> None:
    """Stalled execution with no alive agents → FAILED."""
    exe_id = _create_stalled_execution(exec_manager, temp_db)

    count = await heartbeat.check_stalled_executions()
    assert count == 1

    exe = exec_manager.get_execution(exe_id)
    assert exe is not None
    assert exe.status == ExecutionStatus.FAILED
    assert "stalled" in (exe.outputs_json or "").lower()


@pytest.mark.asyncio
async def test_stalled_with_alive_agents_touches_updated_at(
    heartbeat: PipelineHeartbeat,
    exec_manager: LocalPipelineExecutionManager,
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
) -> None:
    """Stalled execution with alive agents → updated_at refreshed, stays RUNNING."""
    exe_id = _create_stalled_execution(exec_manager, temp_db)
    _add_alive_agent(agent_run_manager)

    old_exe = exec_manager.get_execution(exe_id)
    assert old_exe is not None
    old_updated = old_exe.updated_at

    count = await heartbeat.check_stalled_executions()
    assert count == 1

    exe = exec_manager.get_execution(exe_id)
    assert exe is not None
    assert exe.status == ExecutionStatus.RUNNING
    assert exe.updated_at >= old_updated


@pytest.mark.asyncio
async def test_non_stalled_execution_untouched(
    heartbeat: PipelineHeartbeat,
    exec_manager: LocalPipelineExecutionManager,
) -> None:
    """Execution with recent updated_at is not flagged as stalled."""
    exe = exec_manager.create_execution(
        pipeline_name="test-pipeline",
        session_id=SESSION_ID,
    )
    exec_manager.update_execution_status(exe.id, ExecutionStatus.RUNNING)
    # Don't backdate — it's fresh

    count = await heartbeat.check_stalled_executions()
    assert count == 0

    refreshed = exec_manager.get_execution(exe.id)
    assert refreshed is not None
    assert refreshed.status == ExecutionStatus.RUNNING


@pytest.mark.asyncio
async def test_callable_cron_handler_interface(
    heartbeat: PipelineHeartbeat,
) -> None:
    """Heartbeat is callable with CronJob and returns a string."""
    mock_job = MagicMock()
    result = await heartbeat(mock_job)
    assert isinstance(result, str)
    assert isinstance(result, PipelineHeartbeatResult)
    assert "Heartbeat:" in result
    assert result.should_park is True


@pytest.mark.asyncio
async def test_pipeline_heartbeat_cron_does_not_park_when_idle(
    heartbeat: PipelineHeartbeat,
    temp_db: HubDatabase,
) -> None:
    """Pipeline heartbeat cron execution no longer parks system rows."""
    storage = CronJobStorage(temp_db)
    job = _create_pipeline_heartbeat_job(storage)
    original_next_run = job.next_run_at

    result = await _execute_pipeline_heartbeat_cron(storage, heartbeat, job)

    assert result.status == "completed"
    scheduled = storage.get_job(job.id)
    assert scheduled is not None
    assert scheduled.enabled is True
    assert scheduled.next_run_at == original_next_run


@pytest.mark.asyncio
async def test_pipeline_heartbeat_cron_keeps_schedule_after_stalled_execution(
    heartbeat: PipelineHeartbeat,
    exec_manager: LocalPipelineExecutionManager,
    temp_db: HubDatabase,
) -> None:
    """A handled stalled execution counts as work and does not park the row."""
    _create_stalled_execution(exec_manager, temp_db)
    storage = CronJobStorage(temp_db)
    job = _create_pipeline_heartbeat_job(storage)
    original_next_run = job.next_run_at

    result = await _execute_pipeline_heartbeat_cron(storage, heartbeat, job)

    assert result.status == "completed"
    scheduled = storage.get_job(job.id)
    assert scheduled is not None
    assert scheduled.next_run_at == original_next_run


@pytest.mark.asyncio
async def test_pipeline_heartbeat_cron_keeps_schedule_after_stale_task_recovery(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    temp_db: HubDatabase,
) -> None:
    """A recovered stale claim counts as work and does not park the row."""
    _seed_db(temp_db)
    _create_in_progress_task(task_manager, assignee="sess-does-not-exist")
    storage = CronJobStorage(temp_db)
    job = _create_pipeline_heartbeat_job(storage)
    original_next_run = job.next_run_at

    result = await _execute_pipeline_heartbeat_cron(storage, heartbeat_with_tasks, job)

    assert result.status == "completed"
    scheduled = storage.get_job(job.id)
    assert scheduled is not None
    assert scheduled.next_run_at == original_next_run


# --- Stale task recovery tests ---


@pytest.fixture
def task_manager(temp_db: HubDatabase) -> LocalTaskManager:
    return LocalTaskManager(temp_db)


@pytest.fixture
def agent_run_manager(temp_db: HubDatabase) -> LocalAgentRunManager:
    return LocalAgentRunManager(temp_db)


@pytest.fixture
def session_manager(temp_db: HubDatabase) -> SessionManager:
    return SessionManager(temp_db)


@pytest.fixture
def heartbeat_with_tasks(
    exec_manager: LocalPipelineExecutionManager,
    task_manager: LocalTaskManager,
    agent_run_manager: LocalAgentRunManager,
    session_manager: SessionManager,
) -> PipelineHeartbeat:
    return PipelineHeartbeat(
        execution_manager=exec_manager,
        agent_run_manager=agent_run_manager,
        task_manager=task_manager,
        session_manager=session_manager,
    )


def _create_in_progress_task(
    task_manager: LocalTaskManager,
    project_id: str = PROJECT_ID,
    assignee: str = "agent-dead",
) -> str:
    """Create a task with an in-progress current stage and an owner."""
    task = task_manager.create_task(
        title="Test stale task",
        task_type="task",
        project_id=project_id,
    )
    task_manager.db.execute("UPDATE tasks SET assignee = %s WHERE id = %s", (assignee, task.id))
    if task_manager.db.fetchone("SELECT 1 FROM sessions WHERE id = %s", (assignee,)):
        task_manager.db.execute(
            "UPDATE tasks SET claimed_by_session_id = %s WHERE id = %s",
            (assignee, task.id),
        )
    task_manager.initialize_task_manifest(task.id)
    current_stage = task_manager.stage_states.current_stage(task.id)
    assert current_stage is not None
    task_manager.stage_states.start_stage(
        task.id,
        current_stage.stage_name,
        by_session_id=None,
    )
    return task.id


@pytest.mark.asyncio
async def test_stale_task_with_terminal_agent_run_recovered(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
) -> None:
    """In-progress task with terminal agent run and no live agent moves back to ready."""
    _seed_db(temp_db)
    task_id = _create_in_progress_task(task_manager)

    # Create a terminal (error) agent run for this task
    agent_run_manager.create(
        parent_session_id=SESSION_ID,
        provider="gemini",
        prompt="do stuff",
        task_id=task_id,
    )
    # The run is created as 'pending' — start then fail it
    runs = temp_db.fetchall("SELECT id FROM agent_runs WHERE task_id = %s", (task_id,))
    run_id = runs[0]["id"]
    agent_run_manager.start(run_id)
    agent_run_manager.fail(run_id, error="Agent died")

    recovered = await heartbeat_with_tasks.check_stale_tasks()
    assert recovered == 1

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "ready"
    assert task.assignee is None
    assert task.claimed_by_session_id is None


@pytest.mark.asyncio
async def test_stale_task_with_commits_promoted_to_needs_review(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
) -> None:
    """In-progress task with linked commits but no live agent moves to review."""
    _seed_db(temp_db)
    task_id = _create_in_progress_task(task_manager)

    # Create a terminal agent run
    agent_run_manager.create(
        parent_session_id=SESSION_ID,
        provider="gemini",
        prompt="implement feature",
        task_id=task_id,
    )
    runs = temp_db.fetchall("SELECT id FROM agent_runs WHERE task_id = %s", (task_id,))
    run_id = runs[0]["id"]
    agent_run_manager.start(run_id)
    agent_run_manager.complete(run_id, result="done")

    # Link a commit to the task — agent did real work
    # Write directly to DB since link_commit validates against git
    import json

    row = temp_db.fetchone("SELECT commits FROM tasks WHERE id = %s", (task_id,))
    commits = json.loads(row["commits"]) if row["commits"] else []
    commits.append("abc123de")
    temp_db.execute("UPDATE tasks SET commits = %s WHERE id = %s", (json.dumps(commits), task_id))

    recovered = await heartbeat_with_tasks.check_stale_tasks()
    assert recovered == 1

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "needs_review"
    assert task.assignee is None
    assert task.claimed_by_session_id is None


@pytest.mark.asyncio
async def test_stale_review_task_releases_claim_without_status_regression(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    temp_db: HubDatabase,
) -> None:
    """needs_review task with dead assignee should only clear ownership."""
    _seed_db(temp_db)
    task = task_manager.create_task(
        title="Review me",
        task_type="task",
        project_id=PROJECT_ID,
    )
    task_manager.db.execute(
        "UPDATE tasks SET assignee = %s WHERE id = %s",
        ("sess-does-not-exist", task.id),
    )
    task_manager.initialize_task_manifest(task.id)
    current_stage = task_manager.stage_states.current_stage(task.id)
    assert current_stage is not None
    task_manager.stage_states.start_stage(
        task.id,
        current_stage.stage_name,
        by_session_id=None,
    )
    task_manager.stage_states.submit_for_review(
        task.id,
        current_stage.stage_name,
        by_session_id=None,
    )

    recovered = await heartbeat_with_tasks.check_stale_tasks()
    assert recovered == 1

    updated = task_manager.get_task(task.id)
    assert updated is not None
    assert projected_task_state(updated) == "needs_review"
    assert updated.assignee is None
    assert updated.claimed_by_session_id is None


@pytest.mark.asyncio
async def test_task_with_active_agent_run_not_recovered(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
) -> None:
    """in_progress task with active (running) agent run → not touched."""
    _seed_db(temp_db)
    task_id = _create_in_progress_task(task_manager)

    # Create an active (running) agent run for this task
    run = agent_run_manager.create(
        parent_session_id=SESSION_ID,
        provider="claude",
        prompt="working on it",
        task_id=task_id,
    )
    agent_run_manager.start(run.id)

    recovered = await heartbeat_with_tasks.check_stale_tasks()
    assert recovered == 0

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "in_progress"


@pytest.mark.asyncio
async def test_stale_task_no_managers_returns_zero(
    heartbeat: PipelineHeartbeat,
) -> None:
    """Heartbeat without task/agent_run managers skips stale task check."""
    recovered = await heartbeat.check_stale_tasks()
    assert recovered == 0


# --- Interactive session protection tests ---


@pytest.mark.asyncio
async def test_interactive_session_task_not_recovered(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    temp_db: HubDatabase,
) -> None:
    """in_progress task assigned to a live interactive session → not touched."""
    _seed_db(temp_db)
    # SESSION_ID is seeded as 'active' — simulates an interactive CLI session
    task_id = _create_in_progress_task(task_manager, assignee=SESSION_ID)

    recovered = await heartbeat_with_tasks.check_stale_tasks()
    assert recovered == 0

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "in_progress"
    assert task.assignee == SESSION_ID


@pytest.mark.asyncio
async def test_expired_session_task_recovered(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    temp_db: HubDatabase,
) -> None:
    """In-progress task assigned to an expired session returns to ready."""
    _seed_db(temp_db)
    # Mark the session as expired (simulates SessionLivenessMonitor detecting dead PID)
    temp_db.execute("UPDATE sessions SET status = 'expired' WHERE id = %s", (SESSION_ID,))
    task_id = _create_in_progress_task(task_manager, assignee=SESSION_ID)

    recovered = await heartbeat_with_tasks.check_stale_tasks()
    assert recovered == 1

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "ready"
    assert task.assignee is None
    assert task.claimed_by_session_id is None


@pytest.mark.asyncio
async def test_paused_agent_session_task_recovered(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    temp_db: HubDatabase,
) -> None:
    """In-progress task assigned to a paused agent session (depth > 0) returns to ready."""
    _seed_db(temp_db)
    # Mark session as paused with agent_depth > 0 (dead agent session)
    temp_db.execute(
        "UPDATE sessions SET status = 'paused', agent_depth = 1 WHERE id = %s", (SESSION_ID,)
    )
    task_id = _create_in_progress_task(task_manager, assignee=SESSION_ID)

    recovered = await heartbeat_with_tasks.check_stale_tasks()
    assert recovered == 1

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "ready"
    assert task.assignee is None
    assert task.claimed_by_session_id is None


@pytest.mark.asyncio
async def test_paused_interactive_session_task_not_recovered(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    temp_db: HubDatabase,
) -> None:
    """in_progress task assigned to a paused interactive session (depth 0) → not touched."""
    _seed_db(temp_db)
    # Mark session as paused with agent_depth 0 (interactive user between prompts)
    temp_db.execute(
        "UPDATE sessions SET status = 'paused', agent_depth = 0 WHERE id = %s", (SESSION_ID,)
    )
    task_id = _create_in_progress_task(task_manager, assignee=SESSION_ID)

    recovered = await heartbeat_with_tasks.check_stale_tasks()
    assert recovered == 0

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "in_progress"
    assert task.assignee == SESSION_ID


@pytest.mark.asyncio
async def test_nonexistent_session_task_recovered(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    temp_db: HubDatabase,
) -> None:
    """In-progress task assigned to unknown session returns to ready."""
    _seed_db(temp_db)
    task_id = _create_in_progress_task(task_manager, assignee="sess-does-not-exist")

    recovered = await heartbeat_with_tasks.check_stale_tasks()
    assert recovered == 1

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "ready"
    assert task.assignee is None
    assert task.claimed_by_session_id is None
