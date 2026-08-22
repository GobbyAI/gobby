"""Terminal delivery projection tests for task-close validator runs."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

import gobby.tasks.close_review_delivery as delivery
from gobby.storage.task_close_reviews import TaskCloseReview, TaskCloseReviewStatus

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("run_status", ["error", "timeout", "cancelled"])
def test_failed_validator_run_terminalizes_review_and_clears_lock(
    monkeypatch: pytest.MonkeyPatch,
    run_status: str,
) -> None:
    store = _Store(_review("running"))
    run = SimpleNamespace(status=run_status, error="boom")
    _install(monkeypatch, store=store, run=run, task=None)

    resolved = delivery.terminal_review_delivery(cast(Any, object()), "run")

    assert resolved is not None
    payload, message = resolved
    assert payload["status"] == "error"
    assert payload["closed"] is False
    assert run_status in message
    assert store.finished_status == "error"


def test_interrupted_finalization_recovers_closed_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review("finalizing"))
    run = SimpleNamespace(status="success", error=None)
    task = SimpleNamespace(id="task", commits=["abc"], closed_at=datetime.now(UTC))
    _install(monkeypatch, store=store, run=run, task=task)

    resolved = delivery.terminal_review_delivery(cast(Any, object()), "run")

    assert resolved is not None
    payload, _message = resolved
    assert payload["status"] == "closed"
    assert payload["closed"] is True
    assert payload["commit_shas"] == ["abc"]
    assert store.finished_status == "closed"


class _Store:
    def __init__(self, review: TaskCloseReview) -> None:
        self.review = review
        self.finished_status: str | None = None

    def get_by_run(self, _run_id: str) -> TaskCloseReview:
        return self.review

    def finish(self, _review_id: str, *, status: str, **kwargs: Any) -> TaskCloseReview:
        self.finished_status = status
        self.review = replace(
            self.review,
            status=cast(TaskCloseReviewStatus, status),
            result_payload=dict(kwargs["result_payload"]),
        )
        return self.review


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: _Store,
    run: object,
    task: object | None,
) -> None:
    monkeypatch.setattr(delivery, "TaskCloseReviewStore", lambda _db: store)
    monkeypatch.setattr(
        delivery,
        "LocalAgentRunManager",
        lambda _db: SimpleNamespace(get=lambda _run_id: run),
    )
    monkeypatch.setattr(
        delivery,
        "LocalTaskManager",
        lambda _db: SimpleNamespace(get_task=lambda _task_id: task),
    )


def _review(status: str) -> TaskCloseReview:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    return TaskCloseReview(
        id="review",
        task_id="task",
        task_ref="#42",
        caller_session_id="parent",
        agent_run_id="run",
        close_arguments={},
        review_fingerprint="close",
        evidence_fingerprint="evidence",
        status=cast(TaskCloseReviewStatus, status),
        result_payload=None,
        error=None,
        launched_at=now,
        completed_at=None,
        delivered_at=None,
        created_at=now,
        updated_at=now,
    )
