from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.dispatch import rules
from gobby.dispatch.actions import AdvanceStageAction, EscalateAction, SpawnAgentAction

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
        default_agent=None,
        requires_human=False,
    )


def _task(*stages: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        id="task-pr",
        ref="#99",
        task_type="task",
        stages=list(stages),
        children=[],
        additional_skills=(),
    )


def _context(*, agents: dict[str, object] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        agents=agents or {},
        agent_definitions={},
        stage_registry={},
        children=[],
        prompt_context={},
    )


def test_three_pr_rules_in_correct_order() -> None:
    names = [rule.__name__ for rule in rules.RULES]

    assert names[names.index("holistic_qa_advance_rule") + 1 : names.index("merge_rule")] == [
        "pr_work_rule",
        "pr_review_rule",
        "pr_advance_rule",
    ]


def test_pr_work_escalates_when_no_agent() -> None:
    action = rules.pr_work_rule(_task(_stage("pr", "in_progress")), _context())

    assert isinstance(action, EscalateAction)
    assert action.task_id == "task-pr"
    assert action.reason == "pr_no_agent"


def test_pr_work_spawns_when_merge_orchestrator_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        rules.PROMPT_BUILDERS,
        "merge-orchestrator",
        lambda task, context: "open PR",
    )

    action = rules.pr_work_rule(
        _task(_stage("pr", "in_progress")),
        _context(agents={"merge-orchestrator": {"enabled": True}}),
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.task_id == "task-pr"
    assert action.task_ref == "#99"
    assert action.agent_slug == "merge-orchestrator"
    assert action.initial_variables == {"stage_name": "pr", "stage_state": "in_progress"}


def test_pr_work_escalates_when_merge_orchestrator_disabled() -> None:
    action = rules.pr_work_rule(
        _task(_stage("pr", "in_progress")),
        _context(agents={"merge-orchestrator": {"enabled": False}}),
    )

    assert isinstance(action, EscalateAction)
    assert action.reason == "pr_no_agent"


def test_pr_review_rule_is_no_op() -> None:
    assert rules.pr_review_rule(_task(_stage("pr", "needs_review")), _context()) is None


def test_pr_advance_rule_completes_pr_stage() -> None:
    action = rules.pr_advance_rule(_task(_stage("pr", "review_approved")), _context())

    assert isinstance(action, AdvanceStageAction)
    assert action.task_id == "task-pr"
    assert action.stage_name == "pr"
    assert action.method == "complete_stage"
    assert action.by_session_id == "dispatcher"
