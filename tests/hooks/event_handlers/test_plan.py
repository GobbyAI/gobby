"""Red tests for plan archival on terminal epic state."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
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


@pytest.mark.parametrize(
    "archive_error",
    [
        PermissionError("archive denied"),
        OSError("archive filesystem unavailable"),
        sqlite3.DatabaseError("archive database unavailable"),
    ],
    ids=["permission", "os", "database"],
)
def test_archive_failures_are_logged_and_ignored(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    archive_error: Exception,
) -> None:
    from gobby.hooks.event_handlers import _plan

    def raise_archive_error(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise archive_error

    monkeypatch.setattr(_plan.LocalPlanManager, "archive_plan", raise_archive_error)

    with caplog.at_level(logging.ERROR, logger=_plan.__name__):
        result = _plan.on_epic_terminal(
            SimpleNamespace(task_ref="#200", status="closed", closure_reason="completed"),
            db=object(),
        )

    assert result is None
    assert "Failed to archive plan for terminal epic #200" in caplog.text


def test_archive_cancellation_is_not_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.hooks.event_handlers import _plan

    def raise_cancelled(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise asyncio.CancelledError

    monkeypatch.setattr(_plan.LocalPlanManager, "archive_plan", raise_cancelled)

    with pytest.raises(asyncio.CancelledError):
        _plan.on_epic_terminal(
            SimpleNamespace(task_ref="#200", status="closed", closure_reason="completed"),
            db=object(),
        )
