from __future__ import annotations

from types import SimpleNamespace

import pytest

import gobby.mcp_proxy.tools.tasks._stage_ops as stage_ops
from gobby.dispatch import rules
from gobby.dispatch.actions import AdvanceStageAction, SpawnAgentAction, StartStageAction
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.tasks._crud import update_task
from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
from tests.storage.tasks._stage_test_helpers import (
    initialize_manifest,
    make_task_with_manifest,
    set_stage_state,
    spec,
    stage_row,
    task_row,
)

pytestmark = pytest.mark.unit


def _task_view(task_id: str, stages: object) -> SimpleNamespace:
    return SimpleNamespace(
        id=task_id,
        ref=task_id,
        task_type="feature",
        stages=list(stages),
        children=[],
        additional_skills=(),
    )


def _dispatch_context() -> SimpleNamespace:
    return SimpleNamespace(
        stage_registry={
            "holistic_qa": SimpleNamespace(
                name="holistic_qa",
                default_agent="holistic-reviewer",
                requires_human=False,
                default_max_work_attempts=3,
                default_max_review_rounds=1,
            ),
            "pr": SimpleNamespace(
                name="pr",
                default_agent="merge-orchestrator",
                requires_human=False,
                default_max_work_attempts=3,
                default_max_review_rounds=1,
            ),
            "merge": SimpleNamespace(
                name="merge",
                default_agent="merge-orchestrator",
                requires_human=False,
                default_max_work_attempts=3,
                default_max_review_rounds=1,
            ),
        },
        agents={
            "holistic-reviewer": {"enabled": True},
            "merge-orchestrator": {"enabled": True},
        },
        agent_definitions={},
        children=[],
        prompt_context={},
    )


def _tool_context(temp_db) -> SimpleNamespace:
    return SimpleNamespace(
        task_manager=LocalTaskManager(temp_db),
        resolve_session_id=lambda session_ref: session_ref,
    )


def _record_pr_verdict(temp_db):
    tool = stage_ops.create_stage_ops_registry(_tool_context(temp_db)).get_tool("record_pr_verdict")
    assert tool is not None
    return tool


def _record_merge_result(temp_db):
    tool = stage_ops.create_stage_ops_registry(_tool_context(temp_db)).get_tool(
        "record_merge_result"
    )
    assert tool is not None
    return tool


def test_full_delivery_chain_5_state(temp_db, sample_project) -> None:
    task, manager = make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("holistic_qa", 0), spec("pr", 1), spec("merge", 2)],
        task_type="feature",
    )
    set_stage_state(
        temp_db,
        task.id,
        "holistic_qa",
        "review_approved",
        review_policy="required",
    )
    context = _dispatch_context()

    holistic_done = rules.holistic_qa_advance_rule(
        _task_view(task.id, manager.list_for_task(task.id)),
        context,
    )
    assert isinstance(holistic_done, AdvanceStageAction)
    manager.complete_stage(
        holistic_done.task_id,
        holistic_done.stage_name,
        by_session_id=holistic_done.by_session_id,
    )
    assert stage_row(temp_db, task.id, "holistic_qa")["state"] == "done"
    assert stage_row(temp_db, task.id, "pr")["state"] == "ready"

    pr_start = rules.auto_advance_ready_rule(
        _task_view(task.id, manager.list_for_task(task.id)),
        context,
    )
    assert isinstance(pr_start, StartStageAction)
    assert pr_start.stage_name == "pr"
    manager.start_stage(pr_start.task_id, pr_start.stage_name, by_session_id="dispatcher")
    assert stage_row(temp_db, task.id, "pr")["state"] == "in_progress"

    pr_spawn = rules.pr_work_rule(_task_view(task.id, manager.list_for_task(task.id)), context)
    assert isinstance(pr_spawn, SpawnAgentAction)
    assert pr_spawn.agent_slug == "merge-orchestrator"
    manager.submit_for_review(task.id, "pr", by_session_id="merge-orchestrator")
    assert stage_row(temp_db, task.id, "pr")["state"] == "needs_review"

    _record_pr_verdict(temp_db)(
        task_id=task.id,
        verdict="approve",
        findings="approved",
        report_ref="pr-review.md",
    )
    assert stage_row(temp_db, task.id, "pr")["state"] == "review_approved"

    pr_done = rules.pr_advance_rule(_task_view(task.id, manager.list_for_task(task.id)), context)
    assert isinstance(pr_done, AdvanceStageAction)
    manager.complete_stage(
        pr_done.task_id,
        pr_done.stage_name,
        by_session_id=pr_done.by_session_id,
    )
    assert stage_row(temp_db, task.id, "pr")["state"] == "done"
    assert stage_row(temp_db, task.id, "merge")["state"] == "ready"

    merge_start = rules.auto_advance_ready_rule(
        _task_view(task.id, manager.list_for_task(task.id)),
        context,
    )
    assert isinstance(merge_start, StartStageAction)
    assert merge_start.stage_name == "merge"
    manager.start_stage(
        merge_start.task_id,
        merge_start.stage_name,
        by_session_id="dispatcher",
    )
    assert stage_row(temp_db, task.id, "merge")["state"] == "in_progress"

    merge_spawn = rules.merge_rule(_task_view(task.id, manager.list_for_task(task.id)), context)
    assert isinstance(merge_spawn, SpawnAgentAction)
    assert merge_spawn.agent_slug == "merge-orchestrator"

    _record_merge_result(temp_db)(
        task_id=task.id,
        merge_sha="merge-final123",
        report_ref="merge-report.md",
    )

    assert stage_row(temp_db, task.id, "merge")["state"] == "done"
    row = task_row(temp_db, task.id)
    assert row["closed_at"]
    assert row["closed_reason"] == "manifest_exhausted"
    assert row["closed_commit_sha"] == "merge-final123"


@pytest.mark.asyncio
async def test_parent_holistic_pr_merge_closes_with_real_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
    temp_db,
    sample_project,
) -> None:
    from gobby.agents.sync import sync_bundled_agents
    from gobby.dispatch import dispatcher

    sync_bundled_agents(temp_db)
    manager = LocalTaskManager(temp_db)
    parent = manager.create_task(
        project_id=sample_project["id"],
        title="Parent delivery epic",
        task_type="epic",
        category="planning",
    )
    child = manager.create_task(
        project_id=sample_project["id"],
        title="Closed child leaf",
        parent_task_id=parent.id,
        task_type="task",
        category="code",
    )
    manager.close_task(child.id, force=True)
    update_task(temp_db, parent.id, allow_automation=True, isolation="none")
    initialize_manifest(
        temp_db,
        parent.id,
        [spec("holistic_qa", 0), spec("pr", 1), spec("merge", 2)],
    )
    spawned: list[str] = []
    monkeypatch.setattr(
        dispatcher,
        "spawn_agent",
        lambda action, **kwargs: spawned.append(action.agent_slug) or f"run-{len(spawned)}",
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])
    assert stage_row(temp_db, parent.id, "holistic_qa")["state"] == "in_progress"

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])
    assert spawned[-1] == "holistic-reviewer"
    TaskDispatchMutexManager(temp_db).force_release(parent.id)
    manager.stage_states.submit_for_review(
        parent.id,
        "holistic_qa",
        by_session_id="holistic-reviewer",
    )
    manager.stage_states.approve_review(
        parent.id,
        "holistic_qa",
        by_session_id="holistic-reviewer",
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])
    assert stage_row(temp_db, parent.id, "holistic_qa")["state"] == "done"

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])
    assert stage_row(temp_db, parent.id, "pr")["state"] == "in_progress"

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])
    assert spawned[-1] == "merge-orchestrator"
    TaskDispatchMutexManager(temp_db).force_release(parent.id)
    manager.stage_states.submit_for_review(
        parent.id,
        "pr",
        by_session_id="merge-orchestrator",
    )
    _record_pr_verdict(temp_db)(
        task_id=parent.id,
        verdict="approve",
        findings="approved",
        report_ref="pr-review.md",
    )

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])
    assert stage_row(temp_db, parent.id, "pr")["state"] == "done"

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])
    assert stage_row(temp_db, parent.id, "merge")["state"] == "in_progress"

    await dispatcher.run_heartbeat(db=temp_db, project_id=sample_project["id"])
    assert spawned[-1] == "merge-orchestrator"
    TaskDispatchMutexManager(temp_db).force_release(parent.id)
    _record_merge_result(temp_db)(
        task_id=parent.id,
        merge_sha="merge-parent123",
        report_ref="merge-report.md",
    )

    parent_row = task_row(temp_db, parent.id)
    child_row = task_row(temp_db, child.id)
    assert parent_row["closed_at"] is not None
    assert parent_row["closed_reason"] == "manifest_exhausted"
    assert parent_row["closed_commit_sha"] == "merge-parent123"
    assert child_row["closed_at"] is not None
