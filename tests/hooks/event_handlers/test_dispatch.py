"""Red tests for dispatch mutex event handlers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


def test_terminal_clears_mutex(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.hooks.event_handlers import _dispatch

    calls: list[str] = []
    monkeypatch.setattr(_dispatch.RuntimeDispatchMutex, "force_release_for_run", calls.append)

    _dispatch.on_agent_terminal(SimpleNamespace(run_id="run-1", task_id="task-1"))

    assert calls == ["run-1"]


def test_normal_end_clears_mutex(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.hooks.event_handlers import _dispatch

    calls: list[str] = []
    monkeypatch.setattr(_dispatch.RuntimeDispatchMutex, "force_release_for_run", calls.append)

    _dispatch.on_agent_end_normal(SimpleNamespace(run_id="run-normal", task_id="task-1"))

    assert calls == ["run-normal"]


def test_claim_release_clears_mutex(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.hooks.event_handlers import _dispatch

    calls: list[str] = []
    monkeypatch.setattr(_dispatch.RuntimeDispatchMutex, "force_release_for_run", calls.append)

    _dispatch.on_claim_released(SimpleNamespace(run_id="run-claim", task_id="task-1"))

    assert calls == ["run-claim"]


def test_expansion_completion_advances_lifecycle_when_apply_created_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.hooks.event_handlers import _dispatch

    releases: list[str] = []
    advances: list[tuple[str, str, str]] = []
    monkeypatch.setattr(_dispatch.RuntimeDispatchMutex, "force_release_for_run", releases.append)
    monkeypatch.setattr(
        _dispatch,
        "advance_lifecycle",
        lambda task_id, *, to_lifecycle, to_status, side_effects=None: advances.append(
            (task_id, to_lifecycle, to_status)
        ),
    )

    _dispatch.on_expansion_run_completed("task-1", "expansion-1", apply_created_children=True)

    assert advances == [("task-1", "in_development", "open")]
    assert releases == ["expansion-1"]


def test_compile_only_completion_does_not_advance_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    from gobby.hooks.event_handlers import _dispatch

    releases: list[str] = []
    advances: list[object] = []
    monkeypatch.setattr(_dispatch.RuntimeDispatchMutex, "force_release_for_run", releases.append)
    monkeypatch.setattr(_dispatch, "advance_lifecycle", lambda *args, **kwargs: advances.append(args))

    _dispatch.on_expansion_run_completed("task-1", "expansion-1", apply_created_children=False)

    assert advances == []
    assert releases == ["expansion-1"]


def test_expansion_failure_increments_attempts_and_releases_mutex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.hooks.event_handlers import _dispatch

    releases: list[str] = []
    advances: list[tuple[str, str, str, object]] = []
    monkeypatch.setattr(_dispatch.RuntimeDispatchMutex, "force_release_for_run", releases.append)
    monkeypatch.setattr(
        _dispatch,
        "advance_lifecycle",
        lambda task_id, *, to_lifecycle, to_status, side_effects=None: advances.append(
            (task_id, to_lifecycle, to_status, side_effects)
        ),
    )

    _dispatch.on_expansion_run_failed("task-1", "expansion-1", reason="boom")

    assert advances[0][:3] == ("task-1", "expanding", "open")
    assert "Increment" in type(advances[0][3]).__name__
    assert releases == ["expansion-1"]


def test_expansion_failure_on_exhaust_escalates_or_falls_back() -> None:
    from gobby.dispatch.actions import Action

    from gobby.hooks.event_handlers import _dispatch

    action = _dispatch.on_expansion_run_failed(
        "task-1",
        "expansion-1",
        reason="boom",
        expansion_attempts=3,
        max_expansion_attempts=3,
        unattended=False,
    )

    assert isinstance(action, Action)


def test_expansion_cancellation_releases_mutex_without_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.hooks.event_handlers import _dispatch

    releases: list[str] = []
    advances: list[object] = []
    monkeypatch.setattr(_dispatch.RuntimeDispatchMutex, "force_release_for_run", releases.append)
    monkeypatch.setattr(_dispatch, "advance_lifecycle", lambda *args, **kwargs: advances.append(args))

    _dispatch.on_expansion_run_cancelled("task-1", "expansion-1")

    assert advances == []
    assert releases == ["expansion-1"]


def test_expansion_rule_does_not_refire_after_handler_advances() -> None:
    from gobby.dispatch.rules import evaluate

    from gobby.hooks.event_handlers import _dispatch

    task = SimpleNamespace(lifecycle="expanding", status="open", id="task-1", labels=[])
    _dispatch.on_expansion_run_completed("task-1", "expansion-1", apply_created_children=True)
    task.lifecycle = "in_development"

    assert getattr(evaluate(task, SimpleNamespace()), "kind", None) != "start_expansion"

