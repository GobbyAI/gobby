"""E2E regressions for autonomous build dispatcher handoffs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._stage_ops import create_stage_ops_registry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from gobby.storage.tasks._stage_types import StageManifestSpec
from gobby.utils.session_context import session_context_for_test
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

    registry = create_stage_ops_registry(
        RegistryContext(task_manager=task_manager, sync_manager=cast(Any, SimpleNamespace()))
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
