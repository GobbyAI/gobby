"""Tests for pipeline heartbeat maintenance."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._manager import LocalTaskManager
from gobby.tasks.state_semantics import projected_task_state
from gobby.workflows.pipeline_heartbeat import PipelineHeartbeat
from gobby.workflows.pipeline_state import ExecutionStatus
from tests.fixtures.postgres import TEST_USER_ID

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

# Project/session/task id columns are native uuid in PostgreSQL; synthetic ids
# like "test-project" would fail with `invalid input syntax for type uuid`.
PROJECT_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
MACHINE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab"
SESSION_ID = "11111111-1111-4111-8111-111111111111"
DEAD_AGENT_SESSION_ID = "22222222-2222-4222-8222-222222222222"
STOPPED_SESSION_ID = "33333333-3333-4333-8333-333333333333"
STALE_REVIEW_SESSION_ID = "44444444-4444-4444-8444-444444444444"


def _seed_db(db: HubDatabase) -> None:
    """Insert project + session rows to satisfy FK constraints."""
    db.execute(
        """INSERT INTO projects (id, name, repo_path, created_at, updated_at)
           VALUES (%s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
           ON CONFLICT DO NOTHING""",
        (PROJECT_ID, "test-project", "/tmp/test"),
    )
    db.execute(
        "INSERT INTO machines (id, hostname, owner_user_id) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
        (MACHINE_ID, "test-machine", TEST_USER_ID),
    )
    db.execute(
        """INSERT INTO sessions
           (id, external_id, machine_id, source, project_id, status, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
           ON CONFLICT DO NOTHING""",
        (SESSION_ID, "ext-1", MACHINE_ID, "claude_code", PROJECT_ID, "active"),
    )


def _seed_session(db: HubDatabase, session_id: str, *, status: str = "stopped") -> None:
    db.execute(
        """INSERT INTO sessions
           (id, external_id, machine_id, source, project_id, status, created_at, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
           ON CONFLICT DO NOTHING""",
        (session_id, f"{session_id}-ext", MACHINE_ID, "claude_code", PROJECT_ID, status),
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
    session_id: str | None = SESSION_ID,
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
    run_id: str = "1fbb3f18-f217-5d39-a355-5774741d6228",
) -> None:
    """Create an active agent run in the DB for a parent session."""
    agent_run_manager.create(
        run_id=run_id,
        parent_session_id=parent_session_id,
        provider="test",
        prompt="test agent",
    )
    agent_run_manager.start(run_id)


@pytest.mark.asyncio
async def test_stalled_no_agents_marks_failed(
    heartbeat: PipelineHeartbeat,
    exec_manager: LocalPipelineExecutionManager,
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stalled execution with no alive agents → FAILED."""
    exe_id = _create_stalled_execution(exec_manager, temp_db)

    outcomes: list[tuple[str, str]] = []

    def record_metric(component: str, outcome: str) -> None:
        outcomes.append((component, outcome))

    monkeypatch.setattr(
        "gobby.workflows.pipeline_heartbeat.record_automation_event",
        record_metric,
    )

    count = await heartbeat.check_stalled_executions()
    assert count == 1

    exe = exec_manager.get_execution(exe_id)
    assert exe is not None
    assert exe.status == ExecutionStatus.FAILED
    assert "stalled" in (exe.outputs_json or "").lower()
    assert outcomes == [("pipeline-heartbeat", "failed")]


async def test_stale_pending_execution_fails_and_releases_dispatch_mutex(
    exec_manager: LocalPipelineExecutionManager,
    agent_run_manager: LocalAgentRunManager,
    task_manager: LocalTaskManager,
    temp_db: HubDatabase,
) -> None:
    _seed_db(temp_db)
    task = task_manager.create_task(
        title="Pending pipeline task",
        task_type="task",
        project_id=PROJECT_ID,
        validation_criteria="Test task completion is observable.",
    )
    execution = exec_manager.create_execution(
        pipeline_name="never-started",
        session_id=SESSION_ID,
    )
    temp_db.execute(
        "UPDATE pipeline_executions SET updated_at = NOW() - INTERVAL '5 minutes' WHERE id = %s",
        (execution.id,),
    )
    mutexes = TaskDispatchMutexManager(temp_db)
    assert mutexes.acquire_mutex(
        task.id,
        holder="pipeline-dispatch",
        kind="stage-pipeline:development",
        ttl_seconds=600,
        run_id=execution.id,
    )
    pipeline_heartbeat = PipelineHeartbeat(
        execution_manager=exec_manager,
        agent_run_manager=agent_run_manager,
        stall_threshold_seconds=60,
        task_manager=task_manager,
    )

    handled = await pipeline_heartbeat.check_stalled_executions()

    updated = exec_manager.get_execution(execution.id)
    assert handled == 1
    assert updated is not None
    assert updated.status == ExecutionStatus.FAILED
    assert mutexes.get_mutex_by_run_id(execution.id) is None


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
    assert count == 0

    exe = exec_manager.get_execution(exe_id)
    assert exe is not None
    assert exe.status == ExecutionStatus.RUNNING
    assert exe.updated_at >= old_updated


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stalled_with_agents_under_persisted_child_session_survives(
    heartbeat: PipelineHeartbeat,
    exec_manager: LocalPipelineExecutionManager,
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
) -> None:
    """CLI/HTTP-triggered executions start with session_id=None; once the
    executor persists the pipeline child session, agents spawned under that
    child keep the execution alive instead of being killed as stalled."""
    child_session = "33333333-3333-4333-8333-333333333333"
    _seed_session(temp_db, child_session, status="active")

    exe = exec_manager.create_execution(pipeline_name="test-pipeline", session_id=None)
    exec_manager.update_execution_status(exe.id, ExecutionStatus.RUNNING)
    exec_manager.update_execution_session(exe.id, child_session)
    stale_time = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    temp_db.execute(
        "UPDATE pipeline_executions SET updated_at = %s WHERE id = %s",
        (stale_time, exe.id),
    )
    _add_alive_agent(
        agent_run_manager,
        parent_session_id=child_session,
        run_id="2fbb3f18-f217-5d39-a355-5774741d6229",
    )

    count = await heartbeat.check_stalled_executions()
    assert count == 0

    refreshed = exec_manager.get_execution(exe.id)
    assert refreshed is not None
    assert refreshed.status == ExecutionStatus.RUNNING
    assert refreshed.session_id == child_session


@pytest.mark.asyncio
async def test_stalled_with_active_session_survives_without_agent_runs(
    exec_manager: LocalPipelineExecutionManager,
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
) -> None:
    heartbeat = PipelineHeartbeat(
        execution_manager=exec_manager,
        agent_run_manager=agent_run_manager,
        session_manager=SessionManager(temp_db),
        stall_threshold_seconds=60,
    )
    exe_id = _create_stalled_execution(exec_manager, temp_db)

    count = await heartbeat.check_stalled_executions()

    execution = exec_manager.get_execution(exe_id)
    assert count == 0
    assert execution is not None
    assert execution.status == ExecutionStatus.RUNNING


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stalled_without_session_is_left_unchanged(
    heartbeat: PipelineHeartbeat,
    exec_manager: LocalPipelineExecutionManager,
    temp_db: HubDatabase,
) -> None:
    exe_id = _create_stalled_execution(exec_manager, temp_db, session_id=None)

    count = await heartbeat.check_stalled_executions()

    execution = exec_manager.get_execution(exe_id)
    assert count == 0
    assert execution is not None
    assert execution.status == ExecutionStatus.RUNNING


@pytest.mark.asyncio
async def test_agent_probe_error_does_not_fail_execution(
    heartbeat: PipelineHeartbeat,
    exec_manager: LocalPipelineExecutionManager,
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exe_id = _create_stalled_execution(exec_manager, temp_db)

    def fail_probe(_session_id: str) -> list[object]:
        raise RuntimeError("agent store unavailable")

    monkeypatch.setattr(agent_run_manager, "list_by_parent", fail_probe)

    count = await heartbeat.check_stalled_executions()

    execution = exec_manager.get_execution(exe_id)
    assert count == 0
    assert execution is not None
    assert execution.status == ExecutionStatus.RUNNING


@pytest.mark.asyncio
async def test_stale_snapshot_cannot_overwrite_new_execution_state(
    heartbeat: PipelineHeartbeat,
    exec_manager: LocalPipelineExecutionManager,
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exe_id = _create_stalled_execution(exec_manager, temp_db)
    caplog.set_level(logging.INFO)

    def transition_during_probe(_session_id: str) -> list[object]:
        exec_manager.update_execution_status(exe_id, ExecutionStatus.WAITING_APPROVAL)
        return []

    monkeypatch.setattr(agent_run_manager, "list_by_parent", transition_during_probe)

    count = await heartbeat.check_stalled_executions()

    execution = exec_manager.get_execution(exe_id)
    assert count == 0
    assert execution is not None
    assert execution.status == ExecutionStatus.WAITING_APPROVAL
    assert "state changed since stall scan" in caplog.text


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
    claimed_by_session_id: str = DEAD_AGENT_SESSION_ID,
    max_work_attempts: int | None = None,
) -> str:
    """Create a task with an in-progress current stage and an owner."""
    _seed_session(task_manager.db, claimed_by_session_id)
    task = task_manager.create_task(
        title="Test stale task",
        task_type="task",
        project_id=project_id,
        validation_criteria="Test task completion is observable.",
    )
    task_manager.db.execute(
        "UPDATE tasks SET claimed_by_session_id = %s WHERE id = %s",
        (claimed_by_session_id, task.id),
    )
    task_manager.initialize_task_manifest(task.id)
    current_stage = task_manager.stage_states.current_stage(task.id)
    assert current_stage is not None
    task_manager.stage_states.start_stage(
        task.id,
        current_stage.stage_name,
        by_session_id=None,
    )
    if max_work_attempts is not None:
        task_manager.db.execute(
            """
            UPDATE task_stage_states
               SET max_work_attempts = %s
             WHERE task_id = %s AND stage_name = %s
            """,
            (max_work_attempts, task.id, current_stage.stage_name),
        )
    return task.id


@pytest.mark.asyncio
async def test_stale_task_scan_recovers_oldest_claim_beyond_default_window(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    temp_db: HubDatabase,
) -> None:
    """The oldest orphan is examined even when more than 100 tasks are active."""
    _seed_db(temp_db)
    live_session_id = "55555555-5555-4555-8555-555555555555"
    _seed_session(temp_db, live_session_id, status="active")
    for _ in range(100):
        _create_in_progress_task(task_manager, claimed_by_session_id=live_session_id)

    orphan_task_id = _create_in_progress_task(task_manager)
    temp_db.execute(
        "UPDATE tasks SET updated_at = %s WHERE id = %s",
        ((datetime.now(UTC) - timedelta(days=1)).isoformat(), orphan_task_id),
    )

    result = await heartbeat_with_tasks.check_stale_tasks()
    assert result == (1, 100)

    orphan_task = task_manager.get_task(orphan_task_id)
    assert orphan_task is not None
    assert projected_task_state(orphan_task) == "ready"
    assert orphan_task.claimed_by_session_id is None


@pytest.mark.asyncio
async def test_stale_task_with_terminal_agent_run_recovered(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    agent_run_manager: LocalAgentRunManager,
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-progress task with terminal agent run and no live agent moves back to ready."""
    _seed_db(temp_db)
    task_id = _create_in_progress_task(task_manager, max_work_attempts=1)

    # Create a terminal (error) agent run for this task
    agent_run_manager.create(
        parent_session_id=SESSION_ID,
        provider="codex",
        prompt="do stuff",
        task_id=task_id,
    )
    # The run is created as 'pending' — start then fail it
    runs = temp_db.fetchall("SELECT id FROM agent_runs WHERE task_id = %s", (task_id,))
    run_id = runs[0]["id"]
    agent_run_manager.start(run_id)
    agent_run_manager.fail(run_id, error="Agent died")

    outcomes: list[tuple[str, str]] = []

    def record_metric(component: str, outcome: str) -> None:
        outcomes.append((component, outcome))

    monkeypatch.setattr(
        "gobby.workflows.pipeline_heartbeat.record_automation_event",
        record_metric,
    )

    result = await heartbeat_with_tasks.check_stale_tasks()
    assert result.recovered == 1
    assert outcomes == [("pipeline-heartbeat", "recovered")]

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "ready"
    assert task.claimed_by_session_id is None
    assert task.is_escalated is False
    stage = task_manager.stage_states.get(task_id, "development")
    assert stage is not None
    assert stage.work_attempt_count == 0


@pytest.mark.asyncio
async def test_stale_task_recovery_respects_concurrent_dispatch_mutex(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    temp_db: HubDatabase,
) -> None:
    """A concurrent dispatch lease prevents stale-task recovery writes."""
    _seed_db(temp_db)
    task_id = _create_in_progress_task(task_manager)
    mutexes = TaskDispatchMutexManager(temp_db)
    holder = "concurrent-dispatch"
    assert mutexes.acquire_mutex(
        task_id,
        holder=holder,
        kind="dispatch",
        ttl_seconds=30,
    )

    assert (await heartbeat_with_tasks.check_stale_tasks()).recovered == 0

    task = task_manager.get_task(task_id)
    assert task is not None
    assert task.claimed_by_session_id == DEAD_AGENT_SESSION_ID
    stage = task_manager.stage_states.current_stage(task_id)
    assert stage is not None
    assert stage.state == "in_progress"

    assert mutexes.release_mutex(task_id, holder)
    assert (await heartbeat_with_tasks.check_stale_tasks()).recovered == 1


@pytest.mark.asyncio
async def test_stale_task_recovery_rolls_back_stage_when_claim_release_fails(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stage recovery and claim release commit atomically."""
    _seed_db(temp_db)
    task_id = _create_in_progress_task(task_manager)

    def fail_claim_release(_task_id: str) -> None:
        raise RuntimeError("claim release failed")

    monkeypatch.setattr(task_manager, "release_task_claim", fail_claim_release)

    assert (await heartbeat_with_tasks.check_stale_tasks()).recovered == 0

    task = task_manager.get_task(task_id)
    assert task is not None
    assert task.claimed_by_session_id == DEAD_AGENT_SESSION_ID
    stage = task_manager.stage_states.current_stage(task_id)
    assert stage is not None
    assert stage.state == "in_progress"


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
        provider="codex",
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

    result = await heartbeat_with_tasks.check_stale_tasks()
    assert result.recovered == 1

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "needs_review"
    assert task.claimed_by_session_id is None
    assert task.claimed_by_session_id is None


@pytest.mark.asyncio
async def test_stale_review_task_releases_claim_without_status_regression(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    temp_db: HubDatabase,
) -> None:
    """needs_review task with dead claimed_by_session_id should only clear ownership."""
    _seed_db(temp_db)
    task = task_manager.create_task(
        title="Review me",
        task_type="task",
        project_id=PROJECT_ID,
        validation_criteria="Test task completion is observable.",
    )
    stale_session_id = STALE_REVIEW_SESSION_ID
    _seed_session(task_manager.db, stale_session_id)
    task_manager.db.execute(
        "UPDATE tasks SET claimed_by_session_id = %s WHERE id = %s",
        (stale_session_id, task.id),
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

    result = await heartbeat_with_tasks.check_stale_tasks()
    assert result.recovered == 1

    updated = task_manager.get_task(task.id)
    assert updated is not None
    assert projected_task_state(updated) == "needs_review"
    assert updated.claimed_by_session_id is None
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

    result = await heartbeat_with_tasks.check_stale_tasks()
    assert result.recovered == 0

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "in_progress"


@pytest.mark.asyncio
async def test_stale_task_no_managers_returns_zero(
    heartbeat: PipelineHeartbeat,
) -> None:
    """Heartbeat without task/agent_run managers skips stale task check."""
    result = await heartbeat.check_stale_tasks()
    assert result == (0, 0)


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
    task_id = _create_in_progress_task(task_manager, claimed_by_session_id=SESSION_ID)

    result = await heartbeat_with_tasks.check_stale_tasks()
    assert result.recovered == 0

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "in_progress"
    assert task.claimed_by_session_id == SESSION_ID


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_depth", [0, 1])
async def test_handoff_ready_session_task_not_recovered(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    temp_db: HubDatabase,
    agent_depth: int,
) -> None:
    """A handoff-ready owner retains its in-progress task claim."""
    _seed_db(temp_db)
    temp_db.execute(
        "UPDATE sessions SET status = 'handoff_ready', agent_depth = %s WHERE id = %s",
        (agent_depth, SESSION_ID),
    )
    task_id = _create_in_progress_task(task_manager, claimed_by_session_id=SESSION_ID)

    result = await heartbeat_with_tasks.check_stale_tasks()
    assert result.recovered == 0

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "in_progress"
    assert task.claimed_by_session_id == SESSION_ID


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
    task_id = _create_in_progress_task(
        task_manager,
        claimed_by_session_id=SESSION_ID,
        max_work_attempts=1,
    )

    result = await heartbeat_with_tasks.check_stale_tasks()
    assert result.recovered == 1

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "ready"
    assert task.claimed_by_session_id is None
    assert task.claimed_by_session_id is None
    assert task.is_escalated is False
    stage = task_manager.stage_states.get(task_id, "development")
    assert stage is not None
    assert stage.work_attempt_count == 0


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
    task_id = _create_in_progress_task(task_manager, claimed_by_session_id=SESSION_ID)

    result = await heartbeat_with_tasks.check_stale_tasks()
    assert result.recovered == 1

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "ready"
    assert task.claimed_by_session_id is None
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
    task_id = _create_in_progress_task(task_manager, claimed_by_session_id=SESSION_ID)

    result = await heartbeat_with_tasks.check_stale_tasks()
    assert result.recovered == 0

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "in_progress"
    assert task.claimed_by_session_id == SESSION_ID


@pytest.mark.asyncio
async def test_inactive_session_task_recovered(
    heartbeat_with_tasks: PipelineHeartbeat,
    task_manager: LocalTaskManager,
    temp_db: HubDatabase,
) -> None:
    """In-progress task assigned to an inactive session returns to ready."""
    _seed_db(temp_db)
    task_id = _create_in_progress_task(task_manager, claimed_by_session_id=STOPPED_SESSION_ID)

    result = await heartbeat_with_tasks.check_stale_tasks()
    assert result.recovered == 1

    task = task_manager.get_task(task_id)
    assert task is not None
    assert projected_task_state(task) == "ready"
    assert task.claimed_by_session_id is None
    assert task.claimed_by_session_id is None
