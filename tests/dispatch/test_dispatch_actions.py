"""Red tests for dispatcher action contracts."""

from __future__ import annotations

from dataclasses import asdict

import pytest

pytestmark = pytest.mark.unit


def test_action_round_trip() -> None:
    from gobby.dispatch.actions import Action, SpawnAgentAction

    action: Action = SpawnAgentAction(
        task_id="7d34e462-6ba3-5a6c-b1c6-1584b855cb83",
        task_ref="#1",
        agent_slug="backend-developer",
        prompt="Implement the task",
    )

    assert type(action)(**asdict(action)) == action
