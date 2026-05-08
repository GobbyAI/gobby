from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.dispatch import rules
from gobby.dispatch.actions import EscalateAction, SpawnAgentAction

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
        id="task-merge",
        ref="#merge",
        task_type="feature",
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


def test_merge_rule_in_list() -> None:
    assert rules.merge_rule in rules.RULES
    assert rules.RULES[-1] is rules.merge_rule


def test_merge_rule_escalates_when_merge_agent_missing() -> None:
    action = rules.merge_rule(_task(_stage("merge", "in_progress")), _context())

    assert isinstance(action, EscalateAction)
    assert action.task_id == "task-merge"
    assert action.reason == "merge_no_agent"


def test_merge_rule_spawns_when_merge_agent_registered() -> None:
    action = rules.merge_rule(
        _task(_stage("merge", "in_progress")),
        _context(agents={"merge-orchestrator": {"enabled": True}}),
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.task_id == "task-merge"
    assert action.agent_slug == "merge-orchestrator"
