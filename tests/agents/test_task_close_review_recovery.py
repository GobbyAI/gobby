"""Daemon-start reconciliation tests for durable task-close reviews."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import gobby.runner_lifecycle_agents as lifecycle_agents
from gobby.storage.task_close_reviews import TaskCloseReview, TaskCloseReviewStatus

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_orphaned_launch_becomes_error_and_wakes_origin_without_relaunch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="launching", run_id=None))
    subscribers = _Subscribers()
    wake = AsyncMock(return_value={"ism_persisted": True})
    _install(monkeypatch, store=store, run=None, subscribers=subscribers)

    recovered = await lifecycle_agents._reconcile_task_close_reviews_on_startup(_runner(wake))

    assert recovered == 2
    assert store.finished_status == "error"
    assert store.delivered is True
    wake.assert_awaited_once()
    assert wake.call_args.args[2]["event"] == "task_close_review_completed"
    assert subscribers.added == []


@pytest.mark.asyncio
async def test_running_review_rehydrates_durable_parent_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = _review(status="running", run_id="run")
    store = _Store(review)
    subscribers = _Subscribers()
    run = SimpleNamespace(id="run", status="running")
    wake = AsyncMock()
    _install(monkeypatch, store=store, run=run, subscribers=subscribers)

    recovered = await lifecycle_agents._reconcile_task_close_reviews_on_startup(_runner(wake))

    assert recovered == 0
    assert store.finished_status is None
    assert subscribers.added == [("run", ["parent"])]
    wake.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_payload_without_run_is_redelivered_on_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "event": "task_close_review_completed",
        "review_id": "review",
        "status": "error",
        "message": "launch failed",
    }
    store = _Store(
        replace(
            _review(status="error", run_id=None),
            result_payload=payload,
            completed_at=datetime(2026, 8, 22, tzinfo=UTC),
        )
    )
    wake = AsyncMock(return_value={"ism_persisted": True})
    _install(monkeypatch, store=store, run=None, subscribers=_Subscribers())

    recovered = await lifecycle_agents._reconcile_task_close_reviews_on_startup(_runner(wake))

    assert recovered == 1
    wake.assert_awaited_once_with("parent", "launch failed", payload)
    assert store.delivered is True


@pytest.mark.asyncio
async def test_periodic_reconciliation_expires_review_and_wakes_subscriber(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    review = replace(
        _review(status="running", run_id="run"),
        close_arguments={"_review_deadline_at": (now - timedelta(seconds=1)).isoformat()},
    )
    store = _Store(review)
    subscribers = _Subscribers()
    run = SimpleNamespace(id="run", status="running")
    cleanup = AsyncMock()
    wake = AsyncMock(return_value={"ism_persisted": True})
    _install(monkeypatch, store=store, run=run, subscribers=subscribers)
    monkeypatch.setattr(lifecycle_agents, "utc_now", lambda: now)

    recovered = await lifecycle_agents._reconcile_task_close_reviews(_runner(wake, cleanup=cleanup))

    assert recovered == 2
    cleanup.assert_awaited_once_with(
        run,
        terminal_payload="Task-close validator exceeded its durable deadline.",
        is_timeout=True,
    )
    assert store.finished_status == "error"
    assert store.delivered is True
    wake.assert_awaited_once()
    assert subscribers.removed == [("run", ["parent"])]


@pytest.mark.asyncio
async def test_periodic_reconciliation_delivers_terminal_run_without_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="running", run_id="run"))
    subscribers = _Subscribers()
    run = SimpleNamespace(id="run", status="error")
    wake = AsyncMock(return_value={"ism_persisted": True})
    _install(monkeypatch, store=store, run=run, subscribers=subscribers)

    def terminal_review_delivery(_db: object, _run_id: str) -> tuple[dict[str, Any], str]:
        payload = {
            "event": "task_close_review_completed",
            "review_id": "review",
            "status": "error",
            "message": "validator ended without a verdict",
        }
        store.finish("review", status="error", result_payload=payload)
        return payload, payload["message"]

    monkeypatch.setattr(
        "gobby.tasks.close_review_delivery.terminal_review_delivery",
        terminal_review_delivery,
    )

    recovered = await lifecycle_agents._reconcile_task_close_reviews(_runner(wake))

    assert recovered == 2
    assert store.finished_status == "error"
    assert store.delivered is True
    wake.assert_awaited_once()
    assert subscribers.removed == [("run", ["parent"])]


class _Store:
    def __init__(self, review: TaskCloseReview) -> None:
        self.review = review
        self.finished_status: str | None = None
        self.delivered = False

    def list_reconcilable(self) -> list[TaskCloseReview]:
        return [self.review]

    def finish(self, _review_id: str, *, status: str, **kwargs: Any) -> TaskCloseReview:
        self.finished_status = status
        self.review = replace(
            self.review,
            status=cast(TaskCloseReviewStatus, status),
            result_payload=dict(kwargs["result_payload"]),
        )
        return self.review

    def get(self, _review_id: str) -> TaskCloseReview:
        return self.review

    def mark_delivered(self, _review_id: str) -> bool:
        self.delivered = True
        return True


class _Subscribers:
    def __init__(self) -> None:
        self.added: list[tuple[str, list[str]]] = []
        self.removed: list[tuple[str, list[str] | None]] = []

    def add_completion_subscribers(self, run_id: str, session_ids: list[str]) -> list[str]:
        self.added.append((run_id, session_ids))
        return session_ids

    def remove_completion_subscribers(
        self,
        run_id: str,
        *,
        session_ids: list[str] | None = None,
    ) -> None:
        self.removed.append((run_id, session_ids))


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: _Store,
    run: object | None,
    subscribers: _Subscribers,
) -> None:
    monkeypatch.setattr(
        "gobby.storage.task_close_reviews.TaskCloseReviewStore",
        lambda _db: store,
    )
    monkeypatch.setattr(lifecycle_agents, "LocalAgentRunManager", lambda _db: _Runs(run))
    monkeypatch.setattr(
        lifecycle_agents,
        "CompletionSubscriberManager",
        lambda _db: subscribers,
    )


class _Runs:
    def __init__(self, run: object | None) -> None:
        self.run = run

    def get(self, _run_id: str) -> object | None:
        return self.run


def _runner(wake: AsyncMock, *, cleanup: AsyncMock | None = None) -> Any:
    runner = SimpleNamespace(
        database=object(),
        db_executor=None,
        wake_dispatcher=SimpleNamespace(wake=wake),
    )
    if cleanup is not None:
        runner.agent_lifecycle_monitor = SimpleNamespace(get_cleanup_agent=lambda: cleanup)
    return runner


def _review(*, status: str, run_id: str | None) -> TaskCloseReview:
    now = datetime(2026, 8, 22, tzinfo=UTC)
    return TaskCloseReview(
        id="review",
        task_id="task",
        task_ref="#42",
        caller_session_id="parent",
        agent_run_id=run_id,
        close_arguments={},
        review_fingerprint="close",
        evidence_fingerprint="evidence",
        status=cast(TaskCloseReviewStatus, status),
        result_payload=None,
        error=None,
        launched_at=None,
        completed_at=None,
        delivered_at=None,
        created_at=now,
        updated_at=now,
    )
