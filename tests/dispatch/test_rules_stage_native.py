from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from tests.phase5_contract_helpers import source_text

pytestmark = pytest.mark.unit


def _stage(stage_name: str, state: str, position: int, **overrides: object) -> SimpleNamespace:
    values = {
        "name": stage_name,
        "stage_name": stage_name,
        "state": state,
        "position": position,
        "updated_at": f"2026-05-02T00:00:0{position}+00:00",
        "work_attempt_count": 0,
        "review_round_count": 0,
        "max_work_attempts": None,
        "max_review_rounds": None,
        "requires_human": False,
        "review_policy": "required",
        "reviewer_agent": None,
        "default_agent": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _registry(**overrides: object) -> SimpleNamespace:
    values = {
        "name": "planning",
        "default_agent": "planner",
        "requires_human": False,
        "default_max_work_attempts": 3,
        "default_max_review_rounds": 2,
        "reviewer_agent": "plan-adversary",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _task(**overrides: object) -> SimpleNamespace:
    values = {
        "id": "task-1",
        "ref": "#1",
        "task_type": "task",
        "labels": ["stage-:planning"],
        "is_closed": False,
        "is_escalated": False,
        "allow_automation": True,
        "unattended": False,
        "isolation": "none",
        "stages": [
            _stage("planning", "done", 0),
            _stage("development", "ready", 1),
        ],
        "children": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _context(**overrides: object) -> SimpleNamespace:
    values = {
        "stage_registry": {"planning": _registry(name="planning")},
        "agents": {"planner": {"enabled": True}},
        "children": [],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_manifest_helpers_replace_legacy_state_helpers() -> None:
    from gobby.dispatch import rules

    assert callable(getattr(rules, "task_has_stage", None))
    assert callable(getattr(rules, "current_stage", None))
    assert not hasattr(rules, "_state")
    assert not hasattr(rules, "_stage_skipped")


def test_current_stage_uses_leftmost_non_done_manifest_row() -> None:
    from gobby.dispatch import rules

    task = _task(
        stages=[
            _stage("planning", "done", 0),
            _stage("test_arch", "done", 1),
            _stage("development", "ready", 2),
            _stage("merge", "ready", 3),
        ]
    )

    current = rules.current_stage(task)

    assert current.name == "development"
    assert current.position == 2
    assert rules.task_has_stage(task, "merge") is True
    assert rules.task_has_stage(task, "holistic_qa") is False


def test_task_has_stage_ignores_legacy_stage_skip_labels() -> None:
    from gobby.dispatch import rules

    task = _task(labels=["stage-:development"], stages=[_stage("planning", "ready", 0)])

    assert rules.task_has_stage(task, "planning") is True
    assert rules.task_has_stage(task, "development") is False


def test_auto_advance_first_stage_emits_start_stage_action() -> None:
    from gobby.dispatch import actions, rules

    task = _task(stages=[_stage("planning", "ready", 0, default_agent="planner")])
    context = _context(
        current_stage=task.stages[0],
        stage_registry={"planning": _registry(name="planning", default_agent="planner")},
    )

    action = rules.auto_advance_ready_rule(task, context)

    assert isinstance(action, actions.StartStageAction)
    assert action.task_id == "task-1"
    assert action.stage_name == "planning"


def test_attempt_caps_are_read_from_stage_rows_and_registry_defaults() -> None:
    source = source_text("src/gobby/dispatch/rules.py")

    assert "work_attempt_count" in source
    assert "review_round_count" in source
    assert "max_work_attempts" in source
    assert "max_review_rounds" in source
    assert "dispatch_failure_count" not in source
    assert "validation_fail_count" not in source


def test_no_rule_invokes_stage_states_manager_directly() -> None:
    source = source_text("src/gobby/dispatch/rules.py")

    assert "StageStatesManager" not in source
    forbidden = re.compile(
        r"\.(start_stage|complete_stage|approve_review|submit_for_review|"
        r"reject_review|fail_stage)\("
    )
    assert forbidden.search(source) is None
