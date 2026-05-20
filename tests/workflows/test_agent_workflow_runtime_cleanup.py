"""Agent workflow completion releases dispatch runtime state."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import gobby.mcp_proxy.tools.tasks._stage_ops as stage_ops
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, TaskDispatchMutexManager
from gobby.storage.tasks._crud import update_task
from gobby.storage.workflow_definitions import LocalWorkflowDefinitionManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.definitions import WorkflowInstance
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import WorkflowInstanceManager
from tests.storage.tasks._stage_test_helpers import initialize_manifest, spec, stage_row

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_agent_workflow_completion_clears_mutex_and_workflow_instance(
    temp_db,
    sample_project,
) -> None:
    temp_db.execute(
        """
        INSERT INTO sessions (
            id, external_id, machine_id, source, project_id, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))
        """,
        (
            "child-session",
            "ext-child-session",
            "machine-1",
            "codex",
            sample_project["id"],
        ),
    )
    task = LocalTaskManager(temp_db).create_task(
        project_id=sample_project["id"],
        title="Workflow-owned task",
    )
    mutex = TaskDispatchMutexManager(temp_db)
    mutex.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="spawn_agent",
        run_id="run-456",
        ttl_seconds=300,
    )
    instance_manager = WorkflowInstanceManager(temp_db)
    instance_manager.save_instance(
        WorkflowInstance(
            id="wf-456",
            session_id="child-session",
            workflow_name="tech-writer-steps",
            current_step="terminate",
            step_entered_at=datetime.now(UTC),
        )
    )

    runner = MagicMock()
    runner.run_storage.get_by_session.return_value = SimpleNamespace(id="run-456")
    engine = RuleEngine(db=temp_db, runner=runner)

    with patch(
        "gobby.workflows.engine.enforcement.complete_and_notify_agent_run",
        new_callable=AsyncMock,
        return_value=True,
    ) as complete:
        await engine._complete_agent_workflow_run("child-session", "tech-writer-steps")

    complete.assert_awaited_once()
    assert mutex.get_mutex(task.id) is None
    assert instance_manager.get_active_instances("child-session") == []


@pytest.mark.asyncio
async def test_submit_for_review_handoff_terminates_worker_and_unblocks_reviewer_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher

    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    parent = session_manager.register(
        external_id="parent-session",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
    )
    child = session_manager.register(
        external_id="child-session",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
    )
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Review handoff task",
        task_type="task",
        category="code",
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
        run_id="run-submit",
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
    LocalWorkflowDefinitionManager(temp_db).create(
        name=workflow_name,
        definition_json=json.dumps(workflow_data),
        workflow_type="workflow",
        enabled=True,
    )
    instance_manager = WorkflowInstanceManager(temp_db)
    instance_manager.save_instance(
        WorkflowInstance(
            id="wf-submit",
            session_id=child.id,
            workflow_name=workflow_name,
            current_step="implement",
            step_entered_at=datetime.now(UTC),
            variables={"review_submitted": False},
        )
    )

    registry = stage_ops.create_stage_ops_registry(
        RegistryContext(task_manager=task_manager, sync_manager=SimpleNamespace())
    )
    with session_context_for_test(child.id):
        handoff = registry.get_tool("submit_for_review")(
            task_id=task.id,
            stage_name="development",
            review_notes="ready for QA",
        )
    assert handoff["ok"] is True
    assert mutex.get_mutex(task.id) is None

    runner = SimpleNamespace(
        run_storage=run_manager,
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

    assert response.decision == "allow"
    assert run_manager.get(run.id).status == "success"
    assert task_manager.get_task(task.id).claimed_by_session_id is None
    assert stage_row(temp_db, task.id, "development")["state"] == "needs_review"
    assert mutex.get_mutex(task.id) is None
    assert instance_manager.get_active_instances(child.id) == []

    sync_bundled_agents(temp_db)
    spawned: list[object] = []
    monkeypatch.setattr(
        dispatcher,
        "spawn_agent",
        lambda action, **_kwargs: spawned.append(action) or "run-reviewer",
    )

    result = await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])

    assert result.executed == 1
    assert spawned[0].agent_slug == "qa-reviewer"
    assert mutex.get_mutex(task.id).run_id == "run-reviewer"
