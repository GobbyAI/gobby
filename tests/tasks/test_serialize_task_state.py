"""serialize_task_state emits the Phase 5 stage-native shape."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.tasks.state_semantics import serialize_task_state

pytestmark = pytest.mark.unit


def test_new_shape() -> None:
    task = SimpleNamespace(
        claimed_by_session_id=None,
        closed_at=None,
        is_escalated=False,
        current_stage=SimpleNamespace(name="development", state="ready"),
        active_blocked_by=set(),
    )

    state = serialize_task_state(task)

    assert "lifecycle_stage" not in state
    assert "lifecycle" not in state
    assert state["current_stage"] == {"name": "development", "state": "ready"}
    assert state["is_escalated"] is False
