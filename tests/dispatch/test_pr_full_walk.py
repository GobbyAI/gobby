from __future__ import annotations

from types import SimpleNamespace

import pytest

import gobby.mcp_proxy.tools.tasks._stage_ops as stage_ops
from gobby.dispatch import rules
from gobby.dispatch.actions import AdvanceStageAction, StartStageAction
from gobby.storage.tasks import LocalTaskManager
from tests.storage.tasks._stage_test_helpers import (
    make_task_with_manifest,
    spec,
    stage_row,
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
            "merge": SimpleNamespace(
                name="merge",
                default_agent="merge-worker",
                requires_human=False,
                default_max_work_attempts=3,
                default_max_review_rounds=1,
            )
        },
        agents={"merge-worker": {"enabled": True}},
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


def test_pr_lifecycle_with_rejection_then_approval(temp_db, sample_project) -> None:
    task, manager = make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("pr", 1), spec("merge", 2)],
        task_type="feature",
    )
    record_pr_verdict = _record_pr_verdict(temp_db)

    manager.start_stage(task.id, "pr", by_session_id="dispatcher")
    manager.submit_for_review(task.id, "pr", by_session_id="operator")
    record_pr_verdict(
        task_id=task.id,
        verdict="rejected",
        findings="missing release notes",
    )

    row = stage_row(temp_db, task.id, "pr")
    assert row["state"] == "ready"
    assert row["review_round_count"] == 1

    manager.start_stage(task.id, "pr", by_session_id="dispatcher")
    assert stage_row(temp_db, task.id, "pr")["work_attempt_count"] == 2
    manager.submit_for_review(task.id, "pr", by_session_id="operator")
    record_pr_verdict(
        task_id=task.id,
        verdict="approved",
        findings="approved on second pass",
    )
    assert stage_row(temp_db, task.id, "pr")["state"] == "review_approved"

    advance_action = rules.pr_advance_rule(
        _task_view(task.id, manager.list_for_task(task.id)),
        _dispatch_context(),
    )
    assert isinstance(advance_action, AdvanceStageAction)
    manager.complete_stage(
        advance_action.task_id,
        advance_action.stage_name,
        by_session_id=advance_action.by_session_id,
    )
    assert stage_row(temp_db, task.id, "pr")["state"] == "done"
    assert stage_row(temp_db, task.id, "merge")["state"] == "ready"

    start_action = rules.auto_advance_ready_rule(
        _task_view(task.id, manager.list_for_task(task.id)),
        _dispatch_context(),
    )
    assert isinstance(start_action, StartStageAction)
    manager.start_stage(
        start_action.task_id,
        start_action.stage_name,
        by_session_id="dispatcher",
    )
    assert stage_row(temp_db, task.id, "merge")["state"] == "in_progress"
