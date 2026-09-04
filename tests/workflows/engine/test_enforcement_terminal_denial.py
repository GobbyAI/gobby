"""Regression coverage for terminal enforcement-denial cleanup."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.spawn_agent._spawn_guards import TaskSpawnLease
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, TaskDispatchMutexManager
from gobby.workflows.definitions import WorkflowStep
from gobby.workflows.engine.core import RuleEngine

SESSION_ID = "11111111-1111-4111-8111-111111111111"
MACHINE_ID = "21000000-0000-4000-8000-000000000001"


def _terminal_denial_setup(
    db: Any,
    project_id: str,
) -> tuple[
    RuleEngine,
    LocalAgentRunManager,
    TaskDispatchMutexManager,
    str,
    str,
    WorkflowStep,
    MagicMock,
]:
    db.execute(
        """
        INSERT INTO sessions (
            id, external_id, machine_id, source, project_id, status, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, 'active', NOW(), NOW())
        """,
        (SESSION_ID, "terminal-denial-session", MACHINE_ID, "codex", project_id),
    )
    task = LocalTaskManager(db).create_task(
        project_id=project_id,
        title="Retry immediately after terminal enforcement denial",
        validation_criteria="Terminal denial cleanup releases the task dispatch mutex.",
    )
    run_manager = LocalAgentRunManager(db)
    run = run_manager.create(
        parent_session_id=SESSION_ID,
        child_session_id=SESSION_ID,
        provider="codex",
        prompt="test",
        task_id=task.id,
    )
    assert run_manager.start(run.id) is not None

    mutex_manager = TaskDispatchMutexManager(db)
    assert mutex_manager.acquire_mutex(
        task.id,
        holder="spawn-agent:terminal-denial",
        kind="spawn_agent",
        run_id=run.id,
        ttl_seconds=600,
    )

    runner = MagicMock()
    runner.run_storage = run_manager
    runner.terminal_services = object()
    runner.agent_lifecycle_monitor = None
    engine = RuleEngine(db, runner=runner)
    engine.instance_manager = MagicMock()

    instance = MagicMock()
    instance.id = "22222222-2222-4222-8222-222222222222"
    instance.agent_name = "backend-developer"
    instance.step_entered_at = datetime.now(UTC)
    instance.total_action_count = 0
    instance.variables = {}
    return engine, run_manager, mutex_manager, task.id, run.id, WorkflowStep(name="claim"), instance


def _record_three_identical_denials(
    engine: RuleEngine,
    step: WorkflowStep,
    instance: MagicMock,
) -> str:
    reason = "blocked"
    with engine.db.transaction():
        for _ in range(3):
            reason = engine._record_enforcement_denial(
                session_id=SESSION_ID,
                rule="step-native-tool-allowlist",
                target="tool:edit",
                reason=reason,
                step=step,
                instance=instance,
            )
    return reason


@pytest.mark.asyncio
async def test_third_identical_denial_requests_termination_and_releases_mutex(
    hub_db: Any,
    sample_project: dict[str, Any],
) -> None:
    engine, run_manager, mutex_manager, task_id, run_id, step, instance = _terminal_denial_setup(
        hub_db, str(sample_project["id"])
    )

    reason = _record_three_identical_denials(engine, step, instance)
    assert "third identical denial" in reason.lower()
    active_run = run_manager.get(run_id)
    assert active_run is not None
    assert active_run.status == "running"
    assert mutex_manager.get_mutex(task_id) is not None

    with (
        patch(
            "gobby.agents.kill.kill_agent",
            new_callable=AsyncMock,
            return_value={"success": True},
        ) as terminate_process,
        patch(
            "gobby.mcp_proxy.tools.agent_cancellation.reap_srt_runner_process_tree",
            new_callable=AsyncMock,
        ),
    ):
        await engine._flush_pending_terminal_denial(SESSION_ID)

    terminate_process.assert_awaited_once()
    termination_call = terminate_process.await_args
    assert termination_call is not None
    assert termination_call.kwargs["close_terminal"] is True
    assert engine._runner is not None
    assert termination_call.kwargs["terminal_services"] is engine._runner.terminal_services
    terminal_run = run_manager.get(run_id)
    assert terminal_run is not None
    assert terminal_run.status == "error"
    assert terminal_run.error is not None
    assert "blocked after 3 identical enforcement denials" in terminal_run.error
    assert mutex_manager.get_mutex(task_id) is None
    session = SessionManager(hub_db).get(SESSION_ID)
    assert session is not None
    assert session.status == "expired"


@pytest.mark.asyncio
async def test_immediate_spawn_after_third_denial_acquires_dispatch_mutex(
    hub_db: Any,
    sample_project: dict[str, Any],
) -> None:
    engine, _run_manager, mutex_manager, task_id, _run_id, step, instance = _terminal_denial_setup(
        hub_db, str(sample_project["id"])
    )
    blocked_spawn = TaskSpawnLease(db=hub_db, task_id=task_id)
    blocked = blocked_spawn.acquire()
    assert blocked is not None
    assert blocked["error"] == f"task {task_id} already has an agent spawn in progress"

    _record_three_identical_denials(engine, step, instance)
    with (
        patch(
            "gobby.agents.kill.kill_agent",
            new_callable=AsyncMock,
            return_value={"success": True},
        ),
        patch(
            "gobby.mcp_proxy.tools.agent_cancellation.reap_srt_runner_process_tree",
            new_callable=AsyncMock,
        ),
    ):
        await engine._flush_pending_terminal_denial(SESSION_ID)

    immediate_spawn = TaskSpawnLease(db=hub_db, task_id=task_id)
    assert immediate_spawn.acquire() is None
    assert mutex_manager.get_mutex(task_id) is not None
    immediate_spawn.release_unattached()


@pytest.mark.asyncio
async def test_pending_terminal_denials_are_isolated_by_session(
    hub_db: Any,
) -> None:
    engine = RuleEngine(hub_db)
    first_storage = MagicMock()
    second_storage = MagicMock()
    engine._pending_terminal_denials = {
        "session-one": (MagicMock(id="run-one"), first_storage, "error-one"),
        "session-two": (MagicMock(id="run-two"), second_storage, "error-two"),
    }

    await engine._flush_pending_terminal_denial("session-one")

    first_storage.fail.assert_called_once_with("run-one", "error-one")
    second_storage.fail.assert_not_called()
    assert "session-two" in engine._pending_terminal_denials
