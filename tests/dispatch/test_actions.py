"""Red tests for dispatcher action contracts."""

from __future__ import annotations

from dataclasses import asdict

import pytest

pytestmark = pytest.mark.unit


def test_advance_action_carries_status_fields() -> None:
    from gobby.dispatch.actions import AdvanceLifecycleAction

    action = AdvanceLifecycleAction(
        task_id="task-1",
        from_lifecycle="in_development",
        from_status="needs_review",
        to_lifecycle="holistic_review",
        to_status="review_approved",
        reason="qa-approved",
    )

    assert action.from_lifecycle == "in_development"
    assert action.from_status == "needs_review"
    assert action.to_lifecycle == "holistic_review"
    assert action.to_status == "review_approved"


def test_action_round_trip() -> None:
    from gobby.dispatch.actions import Action, SpawnAgentAction

    action: Action = SpawnAgentAction(
        task_id="task-1",
        task_ref="#1",
        agent_slug="backend-developer",
        prompt="Implement the task",
    )

    assert type(action)(**asdict(action)) == action

