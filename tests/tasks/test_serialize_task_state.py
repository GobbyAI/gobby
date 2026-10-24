"""Task projections expose ownership, closure, escalation and assignment."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gobby.tasks.state_semantics import (
    is_task_actively_claimed,
    serialize_task_state,
)

pytestmark = pytest.mark.unit


def test_new_shape() -> None:
    task = SimpleNamespace(
        claimed_by_session_id=None,
        closed_at=None,
        is_escalated=False,
        active_blocked_by=set(),
    )

    state = serialize_task_state(task)

    assert "lifecycle_stage" not in state
    assert "lifecycle" not in state
    assert set(state) == {
        "name",
        "hold_state",
        "follow_up_at",
        "owner_session_id",
        "is_claimed",
        "is_closed",
        "is_escalated",
        "is_blocked",
        "closed_at",
        "closed_reason",
        "closed_in_session_id",
        "closed_commit_sha",
        "escalated_at",
        "escalation_reason",
        "assigned_agent",
        "implementation_domain",
        "additional_skills",
    }
    assert state["is_escalated"] is False
    assert state["name"] == "open"


def test_awaiting_human_review_label_is_reported_needs_human() -> None:
    awaiting = serialize_task_state(
        SimpleNamespace(labels=["llm-reviewed", "awaiting-human-review"])
    )
    reviewed = serialize_task_state(SimpleNamespace(labels=["llm-reviewed", "human-reviewed"]))

    assert awaiting["name"] == "needs_human"
    assert awaiting["is_blocked"] is False
    assert reviewed["is_blocked"] is False


def test_is_task_actively_claimed_requires_expected_owner() -> None:
    task = SimpleNamespace(
        claimed_by_session_id="owner-1",
        closed_at=None,
        is_escalated=False,
    )

    assert is_task_actively_claimed(task, None) is False
    assert is_task_actively_claimed(task, "owner-1") is True
