"""Focused tests for the pre-adversary plan-enhancement dispatch rule.

The enhancement rule runs as a planning-stage sub-loop that preempts the
adversary while an enhancement budget remains, then falls through to the
unchanged adversary rule once the budget is spent or the plan converges.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gobby.dispatch import rules
from gobby.dispatch._planning_enhancement import planning_enhancement_rule
from gobby.dispatch.actions import SpawnAgentAction

pytestmark = pytest.mark.unit


def _stage(state: str = "needs_review", position: int = 0, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "name": "planning",
        "stage_name": "planning",
        "state": state,
        "position": position,
        "review_round_count": 0,
        "max_review_rounds": None,
        "review_policy": "required",
        "reviewer_agent": "plan-adversary",
        "default_agent": "planner",
        "requires_human": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _artifacts(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "plan_file_path": ".gobby/plans/widget.md",
        "plan_enhancement_rounds": 2,
        "plan_enhancement_rounds_completed": 0,
        "plan_enhancement_converged": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _task(stage: SimpleNamespace | None = None, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": "task-1",
        "ref": "#1",
        "task_type": "task",
        "stages": [stage or _stage()],
        "additional_skills": (),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _registry() -> SimpleNamespace:
    return SimpleNamespace(
        name="planning",
        default_agent="planner",
        reviewer_agent="plan-adversary",
        requires_human=False,
        default_max_work_attempts=3,
        default_max_review_rounds=5,
    )


def _context(artifacts: SimpleNamespace | None = None, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "artifacts": artifacts if artifacts is not None else _artifacts(),
        "agents": {
            "plan-enhancer": {"enabled": True},
            "plan-adversary": {"enabled": True},
        },
        "agent_definitions": {},
        "stage_registry": {"planning": _registry()},
        "current_stage": None,
        "build_config": None,
        "failure_context": None,
        "reason": None,
        "children": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_enhancer_spawns_while_budget_remains() -> None:
    action = planning_enhancement_rule(_task(), _context())

    assert isinstance(action, SpawnAgentAction)
    assert action.agent_slug == "plan-enhancer"
    assert action.task_ref == "#1"
    assert action.initial_variables is not None
    assert action.initial_variables["round_number"] == 1
    assert action.initial_variables["max_enhancement_rounds"] == 2
    assert action.initial_variables["stage_name"] == "planning"
    assert "round 1" in action.prompt
    assert ".gobby/plans/widget.md" in action.prompt


def test_round_number_advances_with_completed() -> None:
    action = planning_enhancement_rule(
        _task(), _context(artifacts=_artifacts(plan_enhancement_rounds_completed=1))
    )

    assert isinstance(action, SpawnAgentAction)
    assert action.initial_variables is not None
    assert action.initial_variables["round_number"] == 2


def test_no_spawn_when_budget_exhausted() -> None:
    ctx = _context(artifacts=_artifacts(plan_enhancement_rounds_completed=2))
    assert planning_enhancement_rule(_task(), ctx) is None


def test_no_spawn_when_converged() -> None:
    ctx = _context(artifacts=_artifacts(plan_enhancement_converged=True))
    assert planning_enhancement_rule(_task(), ctx) is None


def test_no_spawn_when_not_opted_in() -> None:
    ctx = _context(artifacts=_artifacts(plan_enhancement_rounds=0))
    assert planning_enhancement_rule(_task(), ctx) is None


def test_no_spawn_without_plan_artifact() -> None:
    ctx = _context(artifacts=_artifacts(plan_file_path=None))
    assert planning_enhancement_rule(_task(), ctx) is None


def test_no_spawn_when_stage_not_needs_review() -> None:
    task = _task(stage=_stage(state="in_progress"))
    assert planning_enhancement_rule(task, _context()) is None


def test_no_spawn_when_current_stage_is_not_planning() -> None:
    task = _task(
        stages=[
            _stage(state="done", position=0),
            _stage(state="needs_review", position=1, stage_name="development", name="development"),
        ]
    )
    assert planning_enhancement_rule(task, _context()) is None


def test_no_spawn_when_enhancer_agent_missing() -> None:
    ctx = _context(agents={"plan-adversary": {"enabled": True}})
    assert planning_enhancement_rule(_task(), ctx) is None


def test_no_spawn_when_enhancer_agent_disabled() -> None:
    ctx = _context(
        agents={
            "plan-enhancer": {"enabled": False},
            "plan-adversary": {"enabled": True},
        }
    )
    assert planning_enhancement_rule(_task(), ctx) is None


def test_registered_immediately_before_planning_review_rule() -> None:
    base = rules.BASE_RULES
    assert rules.planning_enhancement_rule in base
    assert base.index(rules.planning_enhancement_rule) + 1 == base.index(rules.planning_review_rule)


def test_enhancer_preempts_adversary_while_enabled_then_falls_through() -> None:
    ordered = [rules.planning_enhancement_rule, rules.planning_review_rule]

    # Budget remains: the enhancer preempts the adversary.
    preempt = rules.evaluate(_task(), _context(), rules=ordered)
    assert isinstance(preempt, SpawnAgentAction)
    assert preempt.agent_slug == "plan-enhancer"

    # Converged: enhancement no-ops and dispatch falls through to the adversary.
    converged_ctx = _context(artifacts=_artifacts(plan_enhancement_converged=True))
    fallthrough = rules.evaluate(_task(), converged_ctx, rules=ordered)
    assert isinstance(fallthrough, SpawnAgentAction)
    assert fallthrough.agent_slug == "plan-adversary"
