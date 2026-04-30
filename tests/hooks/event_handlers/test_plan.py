"""Red tests for plan archival on terminal epic state."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def test_completed_plan_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.hooks.event_handlers import _plan

    archived: list[str] = []
    monkeypatch.setattr(
        _plan.LocalPlanManager,
        "archive_plan",
        lambda self, plan_id, **kwargs: archived.append(plan_id),
    )

    _plan.on_epic_terminal(
        SimpleNamespace(task_ref="#200", status="closed", closure_reason="completed"),
        db=object(),
    )

    assert archived == ["#200"]


def test_plan_state_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.hooks.event_handlers import _plan

    archived = SimpleNamespace(state="archived")
    monkeypatch.setattr(_plan.LocalPlanManager, "archive_plan", lambda *args, **kwargs: archived)

    result = _plan.on_epic_terminal(
        SimpleNamespace(task_ref="#200", status="closed", closure_reason="completed"),
        db=object(),
    )

    assert result.state == "archived"
