"""Focused coverage for task-close review progress behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from gobby.autonomous.progress_tracker import (
    ProgressTracker,
    ProgressType,
    _tool_activity_details,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.storage.task_close_reviews import TaskCloseReview, TaskCloseReviewStore
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.close_review_delivery import mark_terminal_review_delivered

pytestmark = pytest.mark.unit


def _create_session(db: HubDatabase) -> str:
    return (
        SessionManager(db)
        .register(
            external_id=f"progress-review-{uuid4()}",
            machine_id=None,
            source="codex",
            project_id=None,
        )
        .id
    )


def _create_active_review(
    db: HubDatabase,
    session_id: str,
    project_id: str,
) -> TaskCloseReview:
    task = LocalTaskManager(db).create_task(
        project_id,
        "Close review progress task",
        validation_criteria="The progress behavior is covered.",
    )
    review, created = TaskCloseReviewStore(db).create_or_get_active(
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        caller_session_id=session_id,
        close_arguments={"preview": False},
        expected_task_updated_at=task.updated_at,
        review_fingerprint="review",
        evidence_fingerprint="evidence",
        diff_sha="d" * 64,
        test_bodies_sha="e" * 64,
        stable_facts={},
    )
    assert created is True
    return review


def test_active_close_review_suppresses_stagnation(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    session_id = _create_session(temp_db)
    tracker = ProgressTracker(temp_db, stagnation_threshold=60)
    tracker.record_event(session_id, ProgressType.FILE_MODIFIED)
    temp_db.execute(
        "UPDATE loop_progress SET recorded_at = %s WHERE session_id = %s",
        ((datetime.now(UTC) - timedelta(seconds=120)).isoformat(), session_id),
    )
    assert tracker.get_summary(session_id).is_stagnant is True

    review = _create_active_review(temp_db, session_id, sample_project["id"])
    assert tracker.get_summary(session_id).is_stagnant is False

    payload = {
        "event": "task_close_review_completed",
        "review_id": review.id,
        "status": "invalid",
    }
    TaskCloseReviewStore(temp_db).finish(
        review.id,
        status="invalid",
        result_payload=payload,
    )
    assert tracker.get_summary(session_id).is_stagnant is True


def test_close_review_delivery_records_progress(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    session_id = _create_session(temp_db)
    review = _create_active_review(temp_db, session_id, sample_project["id"])
    payload = {
        "event": "task_close_review_completed",
        "review_id": review.id,
        "status": "invalid",
    }
    TaskCloseReviewStore(temp_db).finish(
        review.id,
        status="invalid",
        result_payload=payload,
    )

    assert mark_terminal_review_delivered(temp_db, payload, [session_id]) is True

    events = ProgressTracker(temp_db).get_recent_events(session_id, limit=1)
    assert len(events) == 1
    assert events[0].progress_type is ProgressType.TASK_CLOSE_REVIEW_COMPLETED
    assert events[0].tool_name == "task_close_review_completed"
    assert events[0].details == {"review_id": review.id, "status": "invalid"}


@pytest.mark.parametrize(
    ("tool_name", "tool_args"),
    [
        ("Bash", {"command": "sleep 45"}),
        (
            "exec_command",
            {"cmd": "sleep 45", "workdir": "/repo", "yield_time_ms": 30000},
        ),
        ("write_stdin", {"chars": ""}),
    ],
    ids=["claude-bash-sleep", "codex-exec-command-sleep", "codex-empty-write-stdin"],
)
def test_tool_activity_details_classifies_argument_passive_waits(
    tool_name: str,
    tool_args: dict[str, object],
) -> None:
    _, is_passive_wait = _tool_activity_details(tool_name, tool_args)

    assert is_passive_wait is True


@pytest.mark.parametrize(
    ("tool_name", "tool_args"),
    [
        ("Bash", {"command": "sleep 45 && uv run pytest tests/x.py"}),
        ("Bash", {"command": "uv run pytest tests/x.py"}),
        ("write_stdin", {"chars": "continue\n"}),
    ],
    ids=["sleep-command-chain", "non-sleep-command", "non-empty-write-stdin"],
)
def test_tool_activity_details_keeps_active_calls_non_passive(
    tool_name: str,
    tool_args: dict[str, object],
) -> None:
    _, is_passive_wait = _tool_activity_details(tool_name, tool_args)

    assert is_passive_wait is False
