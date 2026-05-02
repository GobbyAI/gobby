from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.dispatch import rules
from gobby.dispatch.actions import AdvanceStageAction, StartStageAction
from tests.storage.tasks._stage_test_helpers import (
    make_task_with_manifest,
    set_stage_state,
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


def _context() -> SimpleNamespace:
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


def test_pr_advance_then_merge_auto_start(temp_db, sample_project) -> None:
    task, manager = make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("pr", 1), spec("merge", 2)],
        task_type="feature",
    )
    set_stage_state(temp_db, task.id, "pr", "review_approved")

    action = rules.pr_advance_rule(_task_view(task.id, manager.list_for_task(task.id)), _context())

    assert isinstance(action, AdvanceStageAction)
    assert action.method == "complete_stage"
    manager.complete_stage(
        action.task_id,
        action.stage_name,
        by_session_id=action.by_session_id,
    )
    assert stage_row(temp_db, task.id, "pr")["state"] == "done"
    assert stage_row(temp_db, task.id, "merge")["state"] == "ready"

    start_action = rules.auto_advance_ready_rule(
        _task_view(task.id, manager.list_for_task(task.id)),
        _context(),
    )

    assert isinstance(start_action, StartStageAction)
    assert start_action.stage_name == "merge"
    manager.start_stage(
        start_action.task_id,
        start_action.stage_name,
        by_session_id="dispatcher",
    )
    assert stage_row(temp_db, task.id, "merge")["state"] == "in_progress"
