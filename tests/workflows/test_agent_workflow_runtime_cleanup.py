"""Agent workflow completion releases dispatch runtime state."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.fixtures.agent_definitions import make_agent_definition

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

from gobby.agents import terminal_delivery
from gobby.agents.agent_cleanup import AgentCleanupHandler
from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, TaskDispatchMutexManager
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.workflows.step_instance_fixtures import make_step_instance

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["none", "before_commit", "shutdown_before_commit"])
async def test_workflow_completion_admits_cleanup_without_waiting_for_it(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    failure_mode: str,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()

    sessions = SessionManager(temp_db)
    child = sessions.register(
        external_id="durable-workflow-child",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Durable workflow cleanup",
        validation_criteria="Owned cleanup releases runtime state after its durable boundary.",
    )
    runs = LocalAgentRunManager(temp_db)
    run = runs.create(
        parent_session_id=child.id,
        child_session_id=child.id,
        provider="codex",
        prompt="complete workflow",
        task_id=task.id,
    )
    runs.start(run.id)
    mutex = TaskDispatchMutexManager(temp_db)
    mutex.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn_agent",
        run_id=run.id,
        ttl_seconds=300,
    )
    instance_manager = AgentStepInstanceManager(temp_db)
    instance_manager.save(
        make_step_instance(child.id, agent_name="durable-cleanup", current_step="terminate")
    )
    cleanup = AgentCleanupHandler(
        agent_run_manager=runs,
        db=temp_db,
        get_session_manager=lambda: None,
        get_session_coordinator=lambda: None,
        clone_storage=None,
        completion_registry=None,
        task_recovery=MagicMock(),
        prompt_detector=MagicMock(),
        terminal_prompt_monitor=MagicMock(),
        stall_classifier=MagicMock(),
        loop_tracker=MagicMock(),
        master_fds={},
    )
    owned_cleanup = cleanup.post_terminal_cleanup

    async def slow_cleanup(*args: Any, **kwargs: Any) -> None:
        started.set()
        await release.wait()
        await owned_cleanup(*args, **kwargs)
        settled.set()

    runner = MagicMock()
    runner.run_storage = runs
    runner.get_run_id_by_session.return_value = run.id
    runner.get_run.side_effect = runs.get
    runner.agent_lifecycle_monitor = cleanup
    engine = RuleEngine(db=temp_db, runner=runner)
    complete_run = runs.complete
    first_commit = True
    commit_started = threading.Event()
    allow_commit = threading.Event()

    def commit(*args: Any, **kwargs: Any) -> Any:
        nonlocal first_commit
        first = first_commit
        first_commit = False
        if first and failure_mode == "before_commit":
            raise RuntimeError("injected completion persistence failure")
        if first and failure_mode == "shutdown_before_commit":
            commit_started.set()
            if not allow_commit.wait(timeout=2):
                raise TimeoutError("test did not release the completion commit")
        return complete_run(*args, **kwargs)

    with (
        patch.object(runs, "complete", side_effect=commit),
        patch.object(cleanup, "post_terminal_cleanup", side_effect=slow_cleanup),
        patch("gobby.workflows.engine.enforcement.cleanup_agent_runtime_state") as clear_runtime,
        patch(
            "gobby.workflows.engine.enforcement.build_workflow_completion_notification",
            return_value=({"success": True}, "Complete"),
        ),
    ):
        if failure_mode == "before_commit":
            with pytest.raises(RuntimeError):
                await engine._complete_agent_workflow_run(child.id, "test", {})
            assert not started.is_set()
            clear_runtime.assert_not_called()
            stored_before_retry = runs.get(run.id)
            assert stored_before_retry is not None
            assert stored_before_retry.status == "running"
        completion = asyncio.create_task(engine._complete_agent_workflow_run(child.id, "test", {}))
        try:
            if failure_mode == "shutdown_before_commit":
                commit_reached = await asyncio.to_thread(commit_started.wait, 2)
                assert commit_reached, "Workflow completion did not reach the commit barrier"
                terminal_delivery.close_terminal_delivery_admission()
                allow_commit.set()
            await asyncio.wait_for(started.wait(), timeout=2)
            done, _ = await asyncio.wait({completion}, timeout=0.1)
            assert completion in done, "Workflow evaluation waited for terminal cleanup"
            assert not settled.is_set()
            clear_runtime.assert_not_called()
            assert mutex.get_mutex(task.id) is not None
            assert instance_manager.get_for_session(child.id) is not None
            stored = runs.get(run.id)
            assert stored is not None
            assert stored.status == "success"
        finally:
            allow_commit.set()
            release.set()
            await completion
            await terminal_delivery.drain_shielded_terminal_deliveries()
            terminal_delivery.reopen_terminal_delivery_admission()
    assert settled.is_set()
    clear_runtime.assert_not_called()
    assert mutex.get_mutex(task.id) is None
    assert instance_manager.get_for_session(child.id) is None


# Session/instance id columns are native uuid in PostgreSQL; synthetic ids
# like "child-session" would fail with `invalid input syntax for type uuid`.
CHILD_SESSION_ID = "11111111-1111-4111-8111-111111111111"
WF_INSTANCE_ID = "22222222-2222-4222-8222-222222222222"
WF_SUBMIT_INSTANCE_ID = "33333333-3333-4333-8333-333333333333"
PARKED_RUN_ID = "44444444-4444-4444-8444-444444444444"

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.mark.asyncio
async def test_persisted_exit_step_retries_failed_completion(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    from gobby.workflows.agent_models import AgentStepWorkflowBody
    from gobby.workflows.definitions import WorkflowStep
    from gobby.workflows.step_instances import build_step_instance

    child = SessionManager(temp_db).register(
        external_id="retry-exit-step",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    instance_manager = AgentStepInstanceManager(temp_db)
    instance_manager.save(
        build_step_instance(
            make_agent_definition(
                name="retry-exit",
                prompts={"persona": "Test", "agent": "Test"},
                surfaces=["spawn"],
                step_workflow=AgentStepWorkflowBody(
                    steps=[WorkflowStep(name="terminate")],
                    exit_condition="current_step == 'terminate'",
                ),
            ),
            session_id=child.id,
            current_step="terminate",
            step_workflow_id=None,
        )
    )
    engine = RuleEngine(db=temp_db, runner=MagicMock())
    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        source=SessionSource.CODEX,
        data={},
        session_id=child.id,
        timestamp=datetime.now(UTC),
    )
    variables: dict[str, Any] = {"step_workflow_complete": False}
    with patch.object(
        engine,
        "_complete_agent_workflow_run",
        side_effect=[RuntimeError("retry completion"), None],
    ) as complete:
        with pytest.raises(RuntimeError, match="retry completion"):
            await engine._process_step_after_tool(event, child.id, variables)
        assert variables["step_workflow_complete"] is False
        persisted = instance_manager.get_for_session(child.id)
        assert persisted is not None and persisted.current_step == "terminate"
        await engine._process_step_after_tool(event, child.id, variables)
        assert variables["step_workflow_complete"] is True
        assert complete.await_count == 2


@pytest.mark.asyncio
async def test_agent_workflow_completion_clears_mutex_and_workflow_instance(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    temp_db.execute(
        """
        INSERT INTO sessions (
            id, external_id, machine_id, source, project_id, status, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            CHILD_SESSION_ID,
            "ext-child-session",
            "21000000-0000-4000-8000-000000000001",
            "codex",
            sample_project["id"],
        ),
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Workflow-owned task",
        validation_criteria="Workflow completion clears its runtime ownership state.",
    )
    mutex = TaskDispatchMutexManager(temp_db)
    mutex.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn_agent",
        run_id="419ed564-7887-5557-8707-10fbb841bcbb",
        ttl_seconds=300,
    )
    instance_manager = AgentStepInstanceManager(temp_db)
    instance_manager.save(
        make_step_instance(
            CHILD_SESSION_ID,
            agent_name="tech-writer",
            current_step="terminate",
        )
    )

    runner = MagicMock()
    runner.run_storage.get_by_session.return_value = SimpleNamespace(
        id="419ed564-7887-5557-8707-10fbb841bcbb"
    )
    engine = RuleEngine(db=temp_db, runner=runner)

    with patch(
        "gobby.workflows.engine.enforcement.complete_and_notify_agent_run",
        new_callable=AsyncMock,
        return_value=True,
    ) as complete:
        await engine._complete_agent_workflow_run(CHILD_SESSION_ID, "tech-writer-steps", {})
        await terminal_delivery.drain_shielded_terminal_deliveries()

    complete.assert_awaited_once()
    assert mutex.get_mutex(task.id) is None
    assert instance_manager.get_for_session(CHILD_SESSION_ID) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("is_interactive", [False, True])
async def test_workflow_terminate_on_parked_daemon_stop_run_retains_state_and_skips_delivery(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    is_interactive: bool,
) -> None:
    """A parked (cancelled/daemon_stop) run must keep its workflow rows and
    never be reported to completion subscribers as a workflow-terminate success."""
    temp_db.execute(
        """
        INSERT INTO sessions (
            id, external_id, machine_id, source, project_id, status, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            CHILD_SESSION_ID,
            "ext-child-session",
            "21000000-0000-4000-8000-000000000001",
            "codex",
            sample_project["id"],
        ),
    )
    instance_manager = AgentStepInstanceManager(temp_db)
    instance_manager.save(
        make_step_instance(
            CHILD_SESSION_ID,
            agent_name="tech-writer",
            current_step="terminate",
        )
    )

    runner = MagicMock()
    # Parked runs are terminal (cancelled), so the running/pending session
    # lookup misses and resolution falls back to get_run_id_by_session.
    runner.run_storage.get_by_session.return_value = None
    runner.get_run_id_by_session.return_value = PARKED_RUN_ID
    runner.get_run.return_value = SimpleNamespace(
        id=PARKED_RUN_ID,
        status="cancelled",
        terminal_reason="daemon_stop",
        child_session_id=CHILD_SESSION_ID,
        is_interactive=is_interactive,
    )
    runner.agent_lifecycle_monitor.complete_workflow_run = AsyncMock()
    engine = RuleEngine(db=temp_db, runner=runner)

    with patch(
        "gobby.workflows.engine.enforcement.complete_and_notify_agent_run",
        new_callable=AsyncMock,
        return_value=True,
    ) as complete:
        await engine._complete_agent_workflow_run(CHILD_SESSION_ID, "tech-writer-steps", {})

    runner.agent_lifecycle_monitor.complete_workflow_run.assert_not_awaited()
    complete.assert_not_awaited()
    active = instance_manager.get_for_session(CHILD_SESSION_ID)
    assert active is not None
    assert active.current_step == "terminate"


def test_daemon_stop_retains_typed_instance_other_reasons_delete(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    temp_db.execute(
        """
        INSERT INTO sessions (
            id, external_id, machine_id, source, project_id, status, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, %s, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            CHILD_SESSION_ID,
            "ext-child-session",
            "21000000-0000-4000-8000-000000000001",
            "codex",
            sample_project["id"],
        ),
    )
    instance_manager = AgentStepInstanceManager(temp_db)
    instance_manager.save(
        make_step_instance(
            CHILD_SESSION_ID,
            agent_name="tech-writer",
            current_step="implement",
            variables={"ticket": "keep"},
        )
    )

    retained = cleanup_agent_runtime_state(
        temp_db,
        run_id=None,
        child_session_id=CHILD_SESSION_ID,
        terminal_reason="daemon_stop",
    )
    kept = instance_manager.get_for_session(CHILD_SESSION_ID)
    assert retained.workflow_instance_rows == 0
    assert kept is not None
    assert kept.current_step == "implement"
    assert kept.variables["ticket"] == "keep"

    deleted = cleanup_agent_runtime_state(
        temp_db,
        run_id=None,
        child_session_id=CHILD_SESSION_ID,
        terminal_reason="user_cancelled",
    )
    assert deleted.workflow_instance_rows == 1
    assert instance_manager.get_for_session(CHILD_SESSION_ID) is None
