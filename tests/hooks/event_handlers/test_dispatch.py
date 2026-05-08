"""Red tests for dispatch mutex event handlers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol
    from gobby.storage.tasks import LocalTaskManager, Task
    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager

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


def test_compile_only_completion_does_not_advance_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.hooks.event_handlers import _dispatch

    releases: list[str] = []
    advances: list[object] = []
    monkeypatch.setattr(_dispatch.RuntimeDispatchMutex, "force_release_for_run", releases.append)
    monkeypatch.setattr(
        _dispatch, "advance_lifecycle", lambda *args, **kwargs: advances.append(args)
    )

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
    from gobby.dispatch.actions import EscalateAction
    from gobby.hooks.event_handlers import _dispatch

    action = _dispatch.on_expansion_run_failed(
        "task-1",
        "expansion-1",
        reason="boom",
        expansion_attempts=3,
        max_expansion_attempts=3,
        unattended=False,
    )

    assert isinstance(action, EscalateAction)


def test_expansion_cancellation_releases_mutex_without_advance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.hooks.event_handlers import _dispatch

    releases: list[str] = []
    advances: list[object] = []
    monkeypatch.setattr(_dispatch.RuntimeDispatchMutex, "force_release_for_run", releases.append)
    monkeypatch.setattr(
        _dispatch, "advance_lifecycle", lambda *args, **kwargs: advances.append(args)
    )

    _dispatch.on_expansion_run_cancelled("task-1", "expansion-1")

    assert advances == []
    assert releases == ["expansion-1"]


def test_expansion_rule_does_not_refire_after_handler_advances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.hooks.event_handlers import _dispatch

    advances: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        _dispatch,
        "advance_lifecycle",
        lambda task_id, *, to_lifecycle, to_status, side_effects=None: advances.append(
            (task_id, to_lifecycle, to_status)
        ),
    )

    _dispatch.on_expansion_run_completed("task-1", "expansion-1", apply_created_children=True)

    assert advances == [("task-1", "in_development", "open")]


def _stage_pipeline_task(
    temp_db: DatabaseProtocol,
    sample_project: dict[str, Any],
    *,
    review_policy: str = "required",
    requested: str = "in_progress",
) -> tuple[LocalTaskManager, Task, TaskDispatchMutexManager]:
    from gobby.storage.tasks import LocalTaskManager
    from gobby.storage.tasks._dispatch_mutex import TaskDispatchMutexManager
    from tests.storage.tasks._stage_test_helpers import initialize_manifest, set_stage_state, spec

    manager = LocalTaskManager(temp_db)
    task = manager.create_task(project_id=sample_project["id"], title="Pipeline stage")
    temp_db.execute(
        "UPDATE task_stages_registry SET review_policy = ? WHERE name = 'expansion'",
        (review_policy,),
    )
    initialize_manifest(temp_db, task.id, [spec("expansion", 0)])
    set_stage_state(temp_db, task.id, "expansion", requested)
    storage = TaskDispatchMutexManager(temp_db)
    storage.ensure_table()
    storage.acquire_mutex(task.id, holder="dispatcher", kind="heartbeat", ttl_seconds=30)
    temp_db.execute(
        """
        UPDATE task_dispatch_mutex
           SET run_id = ?, action_kind = ?
         WHERE task_id = ?
        """,
        ("pe-1", "stage-pipeline:expansion", task.id),
    )
    return manager, task, storage


def test_pipeline_completed_submits_required_stage_for_review(temp_db, sample_project) -> None:
    from gobby.hooks.event_handlers import _dispatch

    manager, task, storage = _stage_pipeline_task(temp_db, sample_project)

    _dispatch.on_pipeline_completed({"execution_id": "pe-1"}, db=temp_db, storage=storage)

    assert manager.stage_states.get(task.id, "expansion").state == "needs_review"
    assert storage.get_mutex(task.id) is None


def test_pipeline_failed_returns_stage_to_ready(temp_db, sample_project) -> None:
    from gobby.hooks.event_handlers import _dispatch

    manager, task, storage = _stage_pipeline_task(temp_db, sample_project)

    _dispatch.on_pipeline_failed(
        {"execution_id": "pe-1", "error": "boom"}, db=temp_db, storage=storage
    )

    stage = manager.stage_states.get(task.id, "expansion")
    assert stage.state == "ready"
    assert storage.get_mutex(task.id) is None


def test_pipeline_cancelled_escalates_stage_and_releases_mutex(temp_db, sample_project) -> None:
    from gobby.hooks.event_handlers import _dispatch

    manager, task, storage = _stage_pipeline_task(temp_db, sample_project)

    _dispatch.on_pipeline_cancelled({"execution_id": "pe-1"}, db=temp_db, storage=storage)

    stage = manager.stage_states.get(task.id, "expansion")
    assert stage.state == "ready"
    assert manager.get_task(task.id).is_escalated is True
    assert storage.get_mutex(task.id) is None


def test_pipeline_failed_illegal_transition_is_ignored_after_mutex_release(
    temp_db,
    sample_project,
) -> None:
    from gobby.hooks.event_handlers import _dispatch

    manager, task, storage = _stage_pipeline_task(
        temp_db,
        sample_project,
        requested="ready",
    )

    result = _dispatch.on_pipeline_failed(
        {"execution_id": "pe-1", "error": "boom"}, db=temp_db, storage=storage
    )

    assert result is None
    assert manager.stage_states.get(task.id, "expansion").state == "ready"
    assert storage.get_mutex(task.id) is None
