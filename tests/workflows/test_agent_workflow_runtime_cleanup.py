"""Agent workflow completion releases dispatch runtime state."""

from __future__ import annotations

import asyncio
import json
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

import gobby.mcp_proxy.tools.tasks._stage_ops as stage_ops
from gobby.agents import terminal_delivery
from gobby.agents.agent_cleanup import AgentCleanupHandler
from gobby.agents.runtime_cleanup import cleanup_agent_runtime_state
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, TaskDispatchMutexManager
from gobby.storage.tasks._updates import update_task
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.storage.tasks._stage_test_helpers import initialize_manifest, spec, stage_row
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
async def test_workflow_terminate_on_parked_daemon_stop_run_retains_state_and_skips_delivery(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
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


@pytest.mark.asyncio
async def test_submit_for_review_handoff_terminates_worker_and_unblocks_reviewer_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher

    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    parent = session_manager.register(
        external_id="parent-session",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    child = session_manager.register(
        external_id="child-session",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Review handoff task",
        task_type="task",
        category="code",
        validation_criteria="Test task completion is observable.",
    )
    update_task(temp_db, task.id, allow_automation=True)
    initialize_manifest(temp_db, task.id, [spec("development", 0)], by_session_id=child.id)
    task_manager.stage_states.start_stage(task.id, "development", by_session_id=child.id)
    task_manager.claim_task(task.id, child.id)

    run_manager = LocalAgentRunManager(temp_db)
    run = run_manager.create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        claimed_session_id=child.id,
        provider="codex",
        prompt="implement",
        run_id="12f470f2-7232-5d24-bc3b-fda2500e4a6e",
        task_id=task.id,
    )
    run_manager.start(run.id)
    mutex = TaskDispatchMutexManager(temp_db)
    mutex.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn_agent",
        run_id=run.id,
        ttl_seconds=300,
    )

    workflow_name = "worker-submit-steps"
    workflow_data = {
        "name": workflow_name,
        "version": "1.0",
        "enabled": False,
        "variables": {"review_submitted": False},
        "steps": [
            {
                "name": "implement",
                "allowed_tools": "all",
                "allowed_mcp_tools": "all",
                "on_mcp_success": [
                    {
                        "server": "gobby-tasks-ops",
                        "tool": "submit_for_review",
                        "action": "set_variable",
                        "variable": "review_submitted",
                        "value": True,
                    }
                ],
                "transitions": [{"to": "terminate", "when": "vars.review_submitted"}],
            },
            {
                "name": "terminate",
                "allowed_tools": "all",
                "allowed_mcp_tools": "all",
            },
        ],
        "exit_condition": "current_step == 'terminate'",
    }
    AgentDefinitionManager(temp_db).create(
        name=workflow_name,
        definition_json=json.dumps(workflow_data),
        enabled=True,
    )
    from gobby.workflows.agent_models import AgentStepWorkflowBody
    from gobby.workflows.step_instances import build_step_instance

    instance_manager = AgentStepInstanceManager(temp_db)
    instance_manager.save(
        build_step_instance(
            make_agent_definition(
                prompts={"persona": "Interactive guidance.", "agent": "Run the assigned task."},
                name="worker-submit",
                surfaces=["spawn"],
                step_workflow=AgentStepWorkflowBody.model_validate(
                    {
                        "variables": {"review_submitted": False},
                        "exit_condition": "current_step == 'terminate'",
                        "steps": workflow_data["steps"],
                    }
                ),
            ),
            session_id=child.id,
            step_workflow_id=None,
            current_step="implement",
            variables={"review_submitted": False},
        )
    )

    registry = stage_ops.create_stage_ops_registry(RegistryContext(task_manager=task_manager))
    submit_review = registry.get_tool("submit_for_review")
    assert submit_review is not None
    with session_context_for_test(child.id):
        handoff = await asyncio.to_thread(
            submit_review,
            task_id=task.id,
            stage_name="development",
            review_notes="ready for QA",
        )
    assert handoff.get("ok") is True, handoff
    assert mutex.get_mutex(task.id) is None

    runner = SimpleNamespace(
        run_storage=run_manager,
        get_run=run_manager.get,
        complete_run=lambda run_id, result=None: run_manager.complete(
            run_id,
            result=result,
        )
        is not None,
    )
    engine = RuleEngine(db=temp_db, runner=runner)
    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        session_id=child.id,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks-ops",
                "tool_name": "submit_for_review",
                "arguments": {"task_id": task.id, "stage_name": "development"},
            },
            "tool_output": {"success": True},
        },
    )

    response = await engine.evaluate(
        event,
        child.id,
        {"is_spawned_agent": True, "step_workflow_complete": False},
    )
    await terminal_delivery.drain_shielded_terminal_deliveries()

    assert response.decision == "allow"
    assert run_manager.get(run.id).status == "success"
    assert task_manager.get_task(task.id).claimed_by_session_id is None
    assert stage_row(temp_db, task.id, "development")["state"] == "needs_review"
    assert mutex.get_mutex(task.id) is None
    assert instance_manager.get_for_session(child.id) is None

    sync_bundled_agents(temp_db)
    spawned: list[object] = []
    monkeypatch.setattr(
        dispatcher,
        "spawn_agent",
        lambda action, **_kwargs: spawned.append(action) or "175b4656-fe55-571c-b57a-44c83644b57e",
    )

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 1
    assert spawned[0].agent_slug == "qa-reviewer"
    assert mutex.get_mutex(task.id).run_id == "175b4656-fe55-571c-b57a-44c83644b57e"


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
