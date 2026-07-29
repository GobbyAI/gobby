"""PostgreSQL concurrency coverage for close-validation terminal transitions."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import (
    LocalTaskManager,
    TaskAlreadyEscalatedError,
    TaskStaleStateError,
    _transitions,
)

UNKNOWN_TASK_ID = "99999999-9999-9999-9999-999999999999"


def test_close_missing_task_raises_not_found(temp_db: HubDatabase) -> None:
    manager = LocalTaskManager(temp_db)

    with pytest.raises(ValueError, match="not found"):
        manager.close_task(UNKNOWN_TASK_ID)


def test_close_existing_task_with_changed_updated_at_raises_stale(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    snapshot = manager.create_task(
        sample_project["id"],
        "Concurrent close",
        validation_criteria="Test task completion is observable.",
    )
    manager.update_task(snapshot.id, title="Changed concurrently")

    with pytest.raises(TaskStaleStateError):
        manager.close_task(snapshot.id, expected_updated_at=snapshot.updated_at)


def _run_concurrently(
    *operations: Callable[[], object],
) -> tuple[list[object], list[BaseException]]:
    start = threading.Barrier(len(operations))
    results: list[object] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run(operation: Callable[[], object]) -> None:
        try:
            start.wait(timeout=5)
            result = operation()
            with lock:
                results.append(result)
        except BaseException as exc:  # pragma: no cover - asserted by callers
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=run, args=(operation,)) for operation in operations]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    return results, errors


def _invalid_transition(
    db: HubDatabase,
    task_id: str,
    *,
    expected_updated_at: Any,
    threshold: int,
) -> tuple[int, bool]:
    return _transitions.increment_validation_failure(
        db,
        task_id,
        expected_updated_at=expected_updated_at,
        threshold=threshold,
        validation_status="invalid",
        validation_feedback="still invalid",
        escalation_reason="validation threshold reached",
    )


def test_concurrent_threshold_crossing_escalates_exactly_once(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Threshold race",
        validation_criteria="Test task completion is observable.",
    )
    manager.update_task(task.id, validation_fail_count=4)
    snapshot = manager.get_task(task.id)

    results, errors = _run_concurrently(
        lambda: _invalid_transition(
            temp_db,
            task.id,
            expected_updated_at=snapshot.updated_at,
            threshold=5,
        ),
        lambda: _invalid_transition(
            temp_db,
            task.id,
            expected_updated_at=snapshot.updated_at,
            threshold=5,
        ),
    )

    assert results == [(5, True)]
    assert len(errors) == 1 and isinstance(errors[0], TaskAlreadyEscalatedError)
    escalated = manager.get_task(task.id)
    assert escalated.validation_fail_count == 5
    assert escalated.is_escalated is True
    assert escalated.claimed_by_session_id is None


def test_increment_failure_on_escalated_task_reports_terminal_state(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Already escalated",
        validation_criteria="Test task completion is observable.",
    )
    manager.update_task(task.id, validation_fail_count=4)
    manager.escalate_task(task.id, reason="manual escalation")
    snapshot = manager.get_task(task.id)

    with pytest.raises(TaskAlreadyEscalatedError):
        _invalid_transition(
            temp_db,
            task.id,
            expected_updated_at=snapshot.updated_at,
            threshold=5,
        )

    assert manager.get_task(task.id).validation_fail_count == 4


def test_increment_failure_on_closed_task_reports_closed_state(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Already closed",
        validation_criteria="Test task completion is observable.",
    )
    closed = _transitions.close_task(
        temp_db,
        task.id,
        expected_updated_at=task.updated_at,
    )

    with pytest.raises(ValueError, match="task is closed"):
        _invalid_transition(
            temp_db,
            task.id,
            expected_updated_at=closed.updated_at,
            threshold=5,
        )


def test_count_already_past_threshold_still_escalates(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Past threshold",
        validation_criteria="Test task completion is observable.",
    )
    manager.update_task(task.id, validation_fail_count=6)
    snapshot = manager.get_task(task.id)

    result = _invalid_transition(
        temp_db,
        task.id,
        expected_updated_at=snapshot.updated_at,
        threshold=5,
    )

    assert result == (7, True)
    assert manager.get_task(task.id).is_escalated is True


@pytest.mark.parametrize("winner", ["valid", "invalid"])
def test_concurrent_valid_and_invalid_verdict_first_transition_wins(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    winner: str,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        f"Verdict race {winner}",
        validation_criteria="Test task completion is observable.",
    )
    manager.update_task(task.id, validation_fail_count=3)
    snapshot = manager.get_task(task.id)
    winner_done = threading.Event()

    def valid() -> object:
        if winner != "valid":
            assert winner_done.wait(timeout=5)
        try:
            return _transitions.close_task(
                temp_db,
                task.id,
                expected_updated_at=snapshot.updated_at,
                reset_validation_fail_count=True,
                validation_status="valid",
                validation_feedback="all criteria satisfied",
            )
        finally:
            if winner == "valid":
                winner_done.set()

    def invalid() -> object:
        if winner != "invalid":
            assert winner_done.wait(timeout=5)
        try:
            return _invalid_transition(
                temp_db,
                task.id,
                expected_updated_at=snapshot.updated_at,
                threshold=1,
            )
        finally:
            if winner == "invalid":
                winner_done.set()

    results, errors = _run_concurrently(valid, invalid)

    assert len(results) == 1
    assert len(errors) == 1
    if winner == "valid":
        assert isinstance(errors[0], ValueError)
        assert "task is closed" in str(errors[0])
    else:
        assert isinstance(errors[0], TaskStaleStateError)
    final = manager.get_task(task.id)
    if winner == "valid":
        assert final.closed_at is not None
        assert final.validation_status == "valid"
        assert final.validation_fail_count == 0
        assert final.is_escalated is False
    else:
        assert final.closed_at is None
        assert final.validation_status == "invalid"
        assert final.validation_fail_count == 4
        assert final.is_escalated is True


def test_deliberate_close_of_escalated_task_succeeds_and_clears_escalation(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    """A caller that read the escalated row may close it; only racing closes lose."""
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Escalated resolution close",
        validation_criteria="Test task completion is observable.",
    )
    manager.escalate_task(task.id, reason="ready for QA review")
    snapshot = manager.get_task(task.id)

    closed = _transitions.close_task(
        temp_db,
        task.id,
        expected_updated_at=snapshot.updated_at,
        reset_validation_fail_count=True,
        validation_status="valid",
        validation_feedback="QA approved",
    )

    assert closed.closed_at is not None
    assert closed.is_escalated is False
    assert closed.escalated_at is None

    with pytest.raises(TaskStaleStateError):
        _transitions.close_task(
            temp_db,
            task.id,
            expected_updated_at=snapshot.updated_at,
        )


def test_manual_escalation_reopen_resets_validation_fail_count(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Manual reopen",
        validation_criteria="Test task completion is observable.",
    )
    manager.update_task(task.id, validation_fail_count=4)
    manager.escalate_task(task.id, reason="manual review")

    reopened = manager.reopen_task(task.id, reason="review resolved")

    assert reopened.validation_fail_count == 0
    assert reopened.is_escalated is False


def test_repeated_close_preserves_terminal_timestamps_and_stage_metadata(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        sample_project["id"],
        "Idempotent close",
        validation_criteria="Test task completion is observable.",
    )
    manager.initialize_task_manifest(task.id)

    first = manager.close_task(task.id, reason="finished")
    first_stage = first.stages[-1]
    second = manager.close_task(task.id, reason="ignored retry")
    second_stage = second.stages[-1]

    assert second.closed_at == first.closed_at
    assert second.updated_at == first.updated_at
    assert second.closed_reason == first.closed_reason == "finished"
    assert second_stage.stage_name == first_stage.stage_name
    assert second_stage.state == first_stage.state
    assert second_stage.completed_at == first_stage.completed_at
    assert second_stage.completed_by_session_id == first_stage.completed_by_session_id
    assert second_stage.completed_commit_sha == first_stage.completed_commit_sha
    assert second_stage.updated_at == first_stage.updated_at
