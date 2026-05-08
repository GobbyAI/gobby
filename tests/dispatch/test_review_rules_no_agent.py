from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

from gobby.dispatch import rules
from gobby.dispatch.actions import Action, EscalateAction

pytestmark = pytest.mark.unit


def _stage(stage_name: str, state: str, position: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        stage_name=stage_name,
        name=stage_name,
        state=state,
        position=position,
        work_attempt_count=0,
        review_round_count=0,
        max_work_attempts=None,
        max_review_rounds=None,
    )


def _task(*stages: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        id="task-review",
        ref="#review",
        task_type="feature",
        stages=list(stages),
        children=[],
        additional_skills=(),
    )


def _context() -> SimpleNamespace:
    return SimpleNamespace(
        agents={},
        agent_definitions={},
        stage_registry={},
        children=[],
        prompt_context={},
    )


@pytest.mark.parametrize(
    ("rule", "stage", "state", "reason"),
    [
        (rules.expansion_review_rule, "expansion", "needs_review", "expansion_no_reviewer"),
        (rules.development_review_rule, "development", "needs_review", "development_no_reviewer"),
        (rules.holistic_qa_rule, "holistic_qa", "in_progress", "holistic_qa_no_reviewer"),
    ],
)
def test_each_review_rule_escalates_specifically_when_reviewer_missing(
    rule: Callable[[object, object], Action | None],
    stage: str,
    state: str,
    reason: str,
) -> None:
    action = rule(_task(_stage(stage, state)), _context())

    assert isinstance(action, EscalateAction)
    assert action.task_id == "task-review"
    assert action.reason == reason
