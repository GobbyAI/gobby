"""Concurrency contracts for task stage-state transitions."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import pytest

from gobby.storage.tasks._runtime_mutex import RuntimeDispatchMutexError
from tests.storage.tasks._stage_test_helpers import make_task_with_manifest, spec, stage_row

pytestmark = pytest.mark.unit


def _synchronize_first_two_calls(
    current_stage: Callable[[str], object],
) -> Callable[[str], object]:
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    call_count = 0

    def _synchronized_current_stage(task_id: str) -> object:
        nonlocal call_count
        stage = current_stage(task_id)
        with lock:
            call_count += 1
            should_wait = call_count <= 2
        if should_wait:
            barrier.wait(timeout=5)
        return stage

    return _synchronized_current_stage


@pytest.mark.parametrize(
    "session_ids",
    [("first-session", "second-session"), (None, None)],
    ids=["distinct-holders", "same-system-holder"],
)
def test_mutex_serializes_two_stage_state_writers(
    temp_db,
    sample_project,
    monkeypatch: pytest.MonkeyPatch,
    session_ids: tuple[str | None, str | None],
) -> None:
    task, manager = make_task_with_manifest(
        temp_db,
        sample_project,
        [spec("development", 0)],
    )
    rows = manager._transitions.rows
    monkeypatch.setattr(
        rows,
        "current_stage",
        _synchronize_first_two_calls(rows.current_stage),
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                manager.start_stage,
                task.id,
                "development",
                by_session_id=session_id,
            )
            for session_id in session_ids
        ]

    successes = [future.result() for future in futures if future.exception() is None]
    errors = [future.exception() for future in futures if future.exception() is not None]

    assert len(successes) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeDispatchMutexError)
    row = stage_row(temp_db, task.id, "development")
    assert row["state"] == "in_progress"
    assert row["work_attempt_count"] == 1
