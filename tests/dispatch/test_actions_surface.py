from __future__ import annotations

from dataclasses import FrozenInstanceError, fields, is_dataclass
from typing import get_args, get_type_hints

import pytest

pytestmark = pytest.mark.unit


def _action_union_members(action_alias: object) -> set[object]:
    action_value = getattr(action_alias, "__value__", action_alias)
    return set(get_args(action_value))


def test_start_stage_action_shape() -> None:
    import gobby.dispatch.actions as actions

    cls = actions.StartStageAction
    assert is_dataclass(cls)
    assert tuple(field.name for field in fields(cls)) == ("task_id", "stage_name")
    assert cls.__slots__ == ("task_id", "stage_name")

    action = cls(task_id="task-1", stage_name="planning")
    assert action.task_id == "task-1"
    assert action.stage_name == "planning"
    with pytest.raises(FrozenInstanceError):
        action.stage_name = "development"


def test_advance_stage_action_method_literal() -> None:
    import gobby.dispatch.actions as actions

    cls = actions.AdvanceStageAction
    assert is_dataclass(cls)
    assert tuple(field.name for field in fields(cls)) == (
        "task_id",
        "stage_name",
        "method",
        "by_session_id",
    )
    assert cls.__slots__ == (
        "task_id",
        "stage_name",
        "method",
        "by_session_id",
    )

    hints = get_type_hints(cls)
    assert set(get_args(hints["method"])) == {"complete_stage", "approve_review"}

    action = cls(task_id="task-1", stage_name="planning", method="complete_stage")
    assert action.by_session_id == "dispatcher"
    with pytest.raises(FrozenInstanceError):
        action.method = "approve_review"


def test_action_union_includes_stage_actions() -> None:
    import gobby.dispatch.actions as actions

    union_members = _action_union_members(actions.Action)
    assert actions.StartStageAction in union_members
    assert actions.AdvanceStageAction in union_members
    assert "StartStageAction" in actions.__all__
    assert "AdvanceStageAction" in actions.__all__


def test_legacy_action_types_still_present() -> None:
    import gobby.dispatch.actions as actions

    union_members = _action_union_members(actions.Action)
    legacy_names = {
        "SpawnAgentAction",
        "StartExpansionAction",
        "CreateIsolationAction",
        "AdvanceLifecycleAction",
        "AppendAuditMarkerAction",
        "EscalateAction",
    }
    for name in legacy_names:
        assert getattr(actions, name) in union_members
        assert name in actions.__all__
