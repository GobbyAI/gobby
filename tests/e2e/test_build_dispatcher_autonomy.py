"""E2E regressions for autonomous build dispatcher handoffs."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from gobby.agents.lifecycle_monitor import AgentLifecycleMonitor
from gobby.agents.prompt_detector import PromptDetector
from gobby.agents.step_workflow import register_agent_step_workflow
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._stage_types import StageManifestSpec
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.agent_resolver import resolve_agent
from gobby.workflows.definitions import WorkflowInstance
from gobby.workflows.state_manager import SessionVariableManager, WorkflowInstanceManager
from tests._timing import wait_for_async_condition
from tests.storage.tasks._stage_test_helpers import stage_row

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_submit_for_review_autonomously_dispatches_reviewer_without_build_resume(
    temp_db: Any,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP stage handoff should trigger reviewer dispatch without build resume."""
    from gobby.agents.sync import sync_bundled_agents

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    run_manager = LocalAgentRunManager(temp_db)
    worker = session_manager.register(
        external_id="planner-worker",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
        agent_depth=1,
    )
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Build plan",
        task_type="epic",
        category="planning",
    )
    task_manager.update_task(
        task.id,
        allow_automation=True,
        isolation="none",
    )
    task_manager.stage_states.initialize_manifest(
        task.id,
        [StageManifestSpec("planning", 0, max_review_rounds=99)],
        by_session_id=None,
    )
    task_manager.stage_states.start_stage(
        task.id,
        "planning",
        by_session_id=worker.id,
    )

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        run = run_manager.create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=str(kwargs["task_id"]),
            run_id="run-autonomous-reviewer",
        )
        return {"success": True, "run_id": run.id, "isolation": "none"}

    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
        worktree_storage=None,
        clone_storage=None,
        git_manager=None,
        clone_manager=None,
        completion_registry=None,
        config=None,
        code_indexer=None,
    )
    monkeypatch.setattr("gobby.app_context._current_container", services)
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )

    registry_task_manager = LocalTaskManager(temp_db)
    registry = create_stage_ops_registry(
        RegistryContext(
            task_manager=registry_task_manager,
            sync_manager=cast(Any, SimpleNamespace()),
        )
    )
    with session_context_for_test(worker.id):
        result = await registry.call(
            "submit_for_review",
            {
                "task_id": task.id,
                "stage_name": "planning",
                "review_notes": "ready for adversary",
            },
        )

    assert result["ok"] is True
    reviewer = await wait_for_async_condition(
        lambda: run_manager.get("run-autonomous-reviewer"),
        timeout=2.0,
        description="autonomous reviewer dispatch",
    )
    assert reviewer is not None
    mutex = TaskDispatchMutexManager(temp_db).get_mutex(task.id)
    assert stage_row(temp_db, task.id, "planning")["state"] == "needs_review"
    assert reviewer.agent_name == "plan-adversary"
    assert reviewer.task_id == task.id
    assert mutex is not None
    assert mutex.run_id == "run-autonomous-reviewer"


@pytest.mark.asyncio
async def test_cancelled_reviewer_wakes_dispatcher_for_replacement_without_build_resume(
    temp_db: Any,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling an active reviewer should immediately dispatch its replacement."""
    from gobby.agents.sync import sync_bundled_agents

    sync_bundled_agents(temp_db)
    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    run_manager = LocalAgentRunManager(temp_db)
    worker = session_manager.register(
        external_id="review-worker",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
        agent_depth=1,
    )
    reviewer_session = session_manager.register(
        external_id="stale-reviewer",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
        agent_depth=2,
    )
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Reviewable plan",
        task_type="epic",
        category="planning",
    )
    task_manager.update_task(
        task.id,
        allow_automation=True,
        isolation="none",
    )
    task_manager.stage_states.initialize_manifest(
        task.id,
        [StageManifestSpec("planning", 0, max_review_rounds=99)],
        by_session_id=None,
    )
    task_manager.stage_states.start_stage(task.id, "planning", by_session_id=worker.id)
    task_manager.stage_states.submit_for_review(task.id, "planning", by_session_id=worker.id)
    task_manager.release_task_claim(task.id)
    task_manager.claim_task(task.id, reviewer_session.id)

    stale_run = run_manager.create(
        parent_session_id=worker.id,
        child_session_id=reviewer_session.id,
        claimed_session_id=reviewer_session.id,
        provider="codex",
        prompt="review it",
        agent_name="plan-adversary",
        task_id=task.id,
        run_id="run-stale-reviewer",
    )
    run_manager.start(stale_run.id)
    mutex_manager = TaskDispatchMutexManager(temp_db)
    assert mutex_manager.acquire_mutex(
        task.id,
        holder="dispatcher",
        kind="review",
        ttl_seconds=300,
        run_id=stale_run.id,
    )

    async def fake_spawn_agent_impl(**kwargs: object) -> dict[str, object]:
        run = run_manager.create(
            parent_session_id=str(kwargs["parent_session_id"]),
            provider="codex",
            prompt=str(kwargs["prompt"]),
            agent_name=str(kwargs["agent_lookup_name"]),
            task_id=str(kwargs["task_id"]),
            run_id="run-replacement-reviewer",
        )
        return {"success": True, "run_id": run.id, "isolation": "none"}

    services = SimpleNamespace(
        database=temp_db,
        task_manager=task_manager,
        session_manager=session_manager,
        agent_runner=SimpleNamespace(),
        worktree_storage=None,
        clone_storage=None,
        git_manager=None,
        clone_manager=None,
        completion_registry=None,
        config=None,
        code_indexer=None,
    )
    monkeypatch.setattr("gobby.app_context._current_container", services)
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.spawn_agent._implementation.spawn_agent_impl",
        fake_spawn_agent_impl,
    )

    monitor = AgentLifecycleMonitor(
        agent_run_manager=run_manager,
        db=temp_db,
        session_manager=session_manager,
        task_manager=task_manager,
    )

    transitioned = await monitor.terminalize_cancelled_run(
        stale_run.id,
        terminal_reason="user_cancelled",
    )

    replacement = await wait_for_async_condition(
        lambda: run_manager.get("run-replacement-reviewer"),
        timeout=2.0,
        description="replacement reviewer dispatch",
    )
    task_after_cancel = task_manager.get_task(task.id)
    mutex = mutex_manager.get_mutex(task.id)

    assert transitioned is True
    assert task_after_cancel is not None
    assert task_after_cancel.claimed_by_session_id is None
    assert stage_row(temp_db, task.id, "planning")["state"] == "needs_review"
    assert replacement is not None
    assert replacement.agent_name == "plan-adversary"
    assert replacement.task_id == task.id
    assert mutex is not None
    assert mutex.run_id == "run-replacement-reviewer"


@pytest.mark.asyncio
async def test_idle_planner_stage_agent_keeps_periodic_enter_and_gets_handoff_reprompt(
    temp_db: Any,
    sample_project: dict[str, Any],
) -> None:
    """A stalled planner still gets Enter nudges and later a semantic handoff prompt."""
    from gobby.agents.sync import sync_bundled_agents

    sync_bundled_agents(temp_db)
    planner = resolve_agent("planner", temp_db, project_id=sample_project["id"])
    assert planner is not None
    workflow_name = register_agent_step_workflow(planner, temp_db)

    task_manager = LocalTaskManager(temp_db)
    session_manager = SessionManager(temp_db)
    run_manager = LocalAgentRunManager(temp_db)
    parent = session_manager.register(
        external_id="build-coordinator",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
    )
    child = session_manager.register(
        external_id="planner-worker",
        machine_id="machine-1",
        source="codex",
        project_id=sample_project["id"],
        agent_depth=1,
    )
    task = task_manager.create_task(
        project_id=sample_project["id"],
        title="Build plan",
        task_type="epic",
        category="planning",
        claimed_by_session_id=child.id,
    )
    task_manager.update_task(task.id, allow_automation=True)
    task_manager.stage_states.initialize_manifest(
        task.id,
        [StageManifestSpec("planning", 0, max_review_rounds=99)],
        by_session_id=None,
    )
    task_manager.stage_states.start_stage(task.id, "planning", by_session_id=child.id)
    run = run_manager.create(
        parent_session_id=parent.id,
        child_session_id=child.id,
        claimed_session_id=child.id,
        provider="codex",
        prompt="Revise the plan",
        agent_name="planner",
        task_id=task.id,
        run_id="run-idle-planner",
    )
    run_manager.start(run.id)
    run_manager.update_runtime(run.id, tmux_session_name="gobby-idle-planner", pid=12345)
    stored_run = run_manager.get(run.id)
    assert stored_run is not None

    WorkflowInstanceManager(temp_db).save_instance(
        WorkflowInstance(
            id="wf-idle-planner",
            session_id=child.id,
            workflow_name=workflow_name,
            current_step="plan",
            variables={
                "task_claimed": True,
                "skill_loaded": True,
                "plan_handoff_complete": False,
            },
        )
    )
    SessionVariableManager(temp_db).set_variable(child.id, "step_workflow_complete", False)
    temp_db.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(seconds=120)).isoformat(), child.id),
    )

    monitor = AgentLifecycleMonitor(
        agent_run_manager=run_manager,
        db=temp_db,
        session_manager=session_manager,
    )
    mock_tmux = AsyncMock()
    mock_tmux.capture_pane.return_value = "❯\n"
    mock_tmux.send_keys.return_value = True
    monitor._tmux = mock_tmux
    monitor._terminal_prompt_monitor._get_tmux = lambda: mock_tmux
    monitor._idle_check_handler._tmux = mock_tmux
    monitor._idle_detector.get_state(stored_run.id).first_idle_at = time.monotonic() - 120

    assert await monitor.check_periodic_enters() == 1
    mock_tmux.send_keys.assert_called_once_with(
        "gobby-idle-planner",
        PromptDetector.ENTER_KEY,
        literal=False,
    )

    assert await monitor.check_idle_agents() == 1
    sent_prompt = mock_tmux.send_keys.call_args.args[1]
    assert "Workflow: planner-steps. Current step: plan." in sent_prompt
    assert 'submit_for_review(stage_name="planning")' in sent_prompt
    assert "end_agent_run" in sent_prompt
    assert stage_row(temp_db, task.id, "planning")["state"] == "in_progress"
