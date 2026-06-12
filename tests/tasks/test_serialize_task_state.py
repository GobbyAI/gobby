"""serialize_task_state emits the Phase 5 stage-native shape."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.tasks.state_semantics import is_task_actively_claimed, serialize_task_state

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


def test_is_task_actively_claimed_requires_expected_owner() -> None:
    task = SimpleNamespace(
        claimed_by_session_id="owner-1",
        closed_at=None,
        is_escalated=False,
        current_stage=SimpleNamespace(name="development", state="in_progress"),
    )

    assert is_task_actively_claimed(task, None) is False
    assert is_task_actively_claimed(task, "owner-1") is True
