"""Concurrency coverage for atomic task storage mutations."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import (
    LocalTaskManager,
    StageManifestSpec,
    Task,
    TaskAlreadyEscalatedError,
    _lifecycle,
    _transitions,
)

pytestmark = pytest.mark.unit


def _run_concurrently(*operations: Callable[[], object]) -> list[BaseException]:
    start = threading.Barrier(len(operations))
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _run(operation: Callable[[], object]) -> None:
        try:
            start.wait(timeout=5)
            operation()
        except BaseException as exc:  # pragma: no cover - asserted by callers
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=_run, args=(operation,)) for operation in operations]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    return errors


def _synchronize_first_two_reads(
    get_task: Callable[[HubDatabase, str], Task],
) -> Callable[[HubDatabase, str], Task]:
    """Force stale-read implementations to observe the same initial row."""
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    call_count = 0

    def _synchronized_get_task(db: HubDatabase, task_id: str) -> Task:
        nonlocal call_count
        task = get_task(db, task_id)
        with lock:
            call_count += 1
            should_wait = call_count <= 2
        if should_wait:
            barrier.wait(timeout=5)
        return task

    return _synchronized_get_task


def test_concurrent_add_label_preserves_both_labels(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_project["id"], "Concurrent labels")
    monkeypatch.setattr(
        _lifecycle,
        "get_task",
        _synchronize_first_two_reads(_lifecycle.get_task),
    )

    errors = _run_concurrently(
        lambda: _lifecycle.add_label(temp_db, task.id, "covers:first"),
        lambda: _lifecycle.add_label(temp_db, task.id, "covers:second"),
    )

    assert errors == []
    assert set(manager.get_task(task.id).labels or []) == {"covers:first", "covers:second"}


def test_concurrent_link_commit_preserves_both_commits(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_project["id"], "Concurrent commits")
    monkeypatch.setattr(
        _lifecycle,
        "get_task",
        _synchronize_first_two_reads(_lifecycle.get_task),
    )

    with patch("gobby.utils.git.normalize_commit_sha", side_effect=lambda sha, cwd=None: sha):
        errors = _run_concurrently(
            lambda: _lifecycle.link_commit(temp_db, task.id, "commit-a"),
            lambda: _lifecycle.link_commit(temp_db, task.id, "commit-b"),
        )

    assert errors == []
    assert set(manager.get_task(task.id).commits or []) == {"commit-a", "commit-b"}


def test_concurrent_unlink_commit_removes_both_commits(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_project["id"], "Concurrent commit removal")
    with patch("gobby.utils.git.normalize_commit_sha", side_effect=lambda sha, cwd=None: sha):
        _lifecycle.link_commit(temp_db, task.id, "commit-a")
        _lifecycle.link_commit(temp_db, task.id, "commit-b")
        _lifecycle.link_commit(temp_db, task.id, "commit-c")
        monkeypatch.setattr(
            _lifecycle,
            "get_task",
            _synchronize_first_two_reads(_lifecycle.get_task),
        )

        errors = _run_concurrently(
            lambda: _lifecycle.unlink_commit(temp_db, task.id, "commit-a"),
            lambda: _lifecycle.unlink_commit(temp_db, task.id, "commit-b"),
        )

    assert errors == []
    assert manager.get_task(task.id).commits == ["commit-c"]


def test_concurrent_escalate_has_exactly_one_winner(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(sample_project["id"], "Concurrent escalation")
    monkeypatch.setattr(
        _transitions,
        "get_task",
        _synchronize_first_two_reads(_transitions.get_task),
    )

    errors = _run_concurrently(
        lambda: _transitions.escalate_task(temp_db, task.id, reason="first"),
        lambda: _transitions.escalate_task(temp_db, task.id, reason="second"),
    )

    assert len(errors) == 1
    assert isinstance(errors[0], TaskAlreadyEscalatedError)
    escalated = manager.get_task(task.id)
    assert escalated.is_escalated is True
    assert escalated.escalation_reason in {"first", "second"}


def test_submit_for_review_preserves_label_added_during_transition(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = LocalTaskManager(temp_db)
    session = SessionManager(temp_db).register(
        external_id="atomic-review",
        machine_id="test-machine",
        source="codex",
        project_id=sample_project["id"],
    )
    task = manager.create_task(sample_project["id"], "Concurrent review labels")
    manager.stage_states.initialize_manifest(
        task.id,
        [StageManifestSpec("development", 0)],
        by_session_id=session.id,
    )
    manager.stage_states.start_stage(task.id, "development", by_session_id=session.id)
    manager.claim_task(task.id, session.id)
    manager.add_label(task.id, "planning-current-verdict:rejected")
    original_update_task = _transitions.update_task

    def _update_after_concurrent_label(
        db: HubDatabase,
        task_id: str,
        *args: Any,
        **kwargs: Any,
    ) -> bool:
        _lifecycle.add_label(db, task_id, "covers:concurrent")
        return original_update_task(db, task_id, *args, **kwargs)

    monkeypatch.setattr(_transitions, "update_task", _update_after_concurrent_label)

    reviewed = manager.submit_for_review(
        task.id,
        "development",
        by_session_id=session.id,
    )

    assert "covers:concurrent" in (reviewed.labels or [])
    assert "planning-current-verdict:rejected" not in (reviewed.labels or [])
