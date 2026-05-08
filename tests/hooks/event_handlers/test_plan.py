"""Red tests for plan archival on terminal epic state."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, NoReturn

import pytest

pytestmark = pytest.mark.unit


def test_completed_plan_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.hooks.event_handlers import _plan

    archived: list[str] = []

    def archive_plan(_self: Any, plan_id: str, **_kwargs: Any) -> None:
        archived.append(plan_id)

    monkeypatch.setattr(_plan.LocalPlanManager, "archive_plan", archive_plan)

    _plan.on_epic_terminal(
        SimpleNamespace(task_ref="#200", status="closed", closure_reason="completed"),
        db=object(),
    )

    assert archived == ["#200"]


def test_plan_state_archived(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.hooks.event_handlers import _plan

    archived = SimpleNamespace(state="archived")

    def archive_plan(*_args: Any, **_kwargs: Any) -> Any:
        return archived

    monkeypatch.setattr(_plan.LocalPlanManager, "archive_plan", archive_plan)

    result = _plan.on_epic_terminal(
        SimpleNamespace(task_ref="#200", status="closed", closure_reason="completed"),
        db=object(),
    )

    assert result.state == "archived"


def test_missing_plan_file_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.hooks.event_handlers import _plan

    def raise_missing_file(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise FileNotFoundError("plan file not found")

    monkeypatch.setattr(_plan.LocalPlanManager, "archive_plan", raise_missing_file)

    result = _plan.on_epic_terminal(
        SimpleNamespace(task_ref="#200", status="closed", closure_reason="completed"),
        db=object(),
    )

    assert result is None
