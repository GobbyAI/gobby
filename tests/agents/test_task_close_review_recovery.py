"""Daemon-start reconciliation tests for durable task-close reviews."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import gobby.runner_lifecycle_agents as lifecycle_agents
import gobby.tasks.agentic_close_review as agentic_close_review
import gobby.tasks.close_review_delivery as close_review_delivery
from gobby.storage.task_close_reviews import (
    SUBMITTED_VERDICT_KIND,
    TaskCloseReview,
    TaskCloseReviewStatus,
    TaskCloseReviewStore,
)

pytestmark = pytest.mark.unit

_CLOSED_TASK = SimpleNamespace(
    id="task", commits=["abc123"], closed_at=datetime(2026, 9, 8, tzinfo=UTC)
)


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
    assert wake.call_args.args[2]["error_class"] == "retryable_infrastructure"
    assert wake.call_args.args[2]["retry_after"] is not None
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
        "error_class": "retryable_infrastructure",
        "retry_after": "2026-09-13T12:15:00+00:00",
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
@pytest.mark.parametrize("run_status", ["success", "error"])
async def test_periodic_reconciliation_delivers_terminal_run_without_verdict(
    monkeypatch: pytest.MonkeyPatch,
    run_status: str,
) -> None:
    store = _Store(
        replace(
            _review(status="running", run_id="run"),
            close_arguments={"_review_deadline_at": "2020-01-01T00:00:00+00:00"},
        )
    )
    subscribers = _Subscribers()
    run = SimpleNamespace(id="run", status=run_status)
    wake = AsyncMock(return_value={"ism_persisted": True})
    _install(monkeypatch, store=store, run=run, subscribers=subscribers)

    def terminal_review_delivery(_db: object, _run_id: str) -> tuple[dict[str, Any], str]:
        message = "validator ended without a verdict"
        payload = {
            "event": "task_close_review_completed",
            "review_id": "review",
            "status": "error",
            "message": message,
            "error_class": "action_required",
            "retry_after": None,
        }
        store.finish("review", status="error", result_payload=payload)
        return payload, message

    monkeypatch.setattr(
        "gobby.tasks.close_review_delivery.terminal_review_delivery",
        terminal_review_delivery,
    )

    recovered = await lifecycle_agents._reconcile_task_close_reviews(_runner(wake))

    assert recovered == 2
    assert store.finished_status == "error"
    assert store.review.result_payload is not None
    assert store.review.result_payload["error_class"] == "action_required"
    assert store.review.result_payload["retry_after"] is None
    assert store.delivered is True
    wake.assert_awaited_once()
    assert subscribers.removed == [("run", ["parent"])]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run_status,task,startup,expected_status,expected_writes",
    [
        pytest.param("running", None, True, "error", ["finish_orphaned_finalizing"], id="orphaned"),
        pytest.param("cancelled", _CLOSED_TASK, True, "closed", ["finish"], id="run-ended-closed"),
        pytest.param(None, _CLOSED_TASK, True, "closed", ["finish"], id="missing-run-closed"),
        pytest.param("running", None, False, "finalizing", [], id="periodic-tick"),
    ],
)
async def test_close_verdict_survives_daemon_restart(
    monkeypatch: pytest.MonkeyPatch,
    run_status: str | None,
    task: object | None,
    startup: bool,
    expected_status: str,
    expected_writes: list[str],
) -> None:
    """A restart mid-finalization strands neither the review nor its caller.

    The verdict itself dies with the daemon, so recovery is always a fresh
    review; what must survive is the task's ability to close again and a
    payload the caller can act on.
    """
    store = _Store(
        replace(
            _review(status="finalizing", run_id="run"),
            result_payload={"kind": SUBMITTED_VERDICT_KIND, "verdict": {"valid": True}},
        )
    )
    run = SimpleNamespace(id="run", status=run_status) if run_status else None
    wake = AsyncMock(return_value={"ism_persisted": True})
    _install(monkeypatch, store=store, run=run, subscribers=_Subscribers())
    _install_delivery(monkeypatch, store=store, run=run, task=task)

    await lifecycle_agents._reconcile_task_close_reviews(_runner(wake), startup=startup)

    assert store.review.status == expected_status
    assert store.writes == expected_writes
    if not expected_writes:
        # The periodic tick must never write to a finalizing review: that status
        # is held across commit_close, so a live submit_close_review may still
        # be running and would be contradicted by a reconciler write.
        assert store.review.result_payload == {
            "kind": SUBMITTED_VERDICT_KIND,
            "verdict": {"valid": True},
        }
        assert store.delivered is False
        wake.assert_not_awaited()
        return

    # A terminal review releases uq_task_close_reviews_active_task, which is
    # what makes the task closable again.
    assert store.review.active is False
    assert store.delivered is True
    payload = wake.call_args.args[2]
    assert payload["event"] == "task_close_review_completed"
    assert payload["closed"] is (expected_status == "closed")
    if expected_status == "closed":
        assert payload["commit_shas"] == ["abc123"]
    else:
        assert payload["error_class"] == "retryable_infrastructure"
        assert payload["required_actions"]
        # The captured verdict is forensic only; it is never reapplied.
        assert "verdict" not in payload


@pytest.mark.asyncio
async def test_daemon_stop_parked_validator_retries_on_a_short_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validator parked by the daemon's own stop waits seconds, not 900s."""
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    store = _Store(_review(status="running", run_id="run"))
    run = SimpleNamespace(
        id="run",
        status="cancelled",
        error=None,
        terminal_reason="daemon_stop",
        resume_metadata_json=None,
    )
    wake = AsyncMock(return_value={"ism_persisted": True})
    _install(monkeypatch, store=store, run=run, subscribers=_Subscribers())
    _install_delivery(monkeypatch, store=store, run=run, task=None)
    monkeypatch.setattr(agentic_close_review, "utc_now", lambda: now)

    await lifecycle_agents._reconcile_task_close_reviews_on_startup(_runner(wake))

    payload = wake.call_args.args[2]
    assert payload["error_class"] == "retryable_infrastructure"
    assert (
        payload["retry_after"]
        == (
            now + timedelta(seconds=agentic_close_review.CLOSE_REVIEW_DAEMON_STOP_RETRY_SECONDS)
        ).isoformat()
    )
    assert (
        agentic_close_review.CLOSE_REVIEW_DAEMON_STOP_RETRY_SECONDS
        < agentic_close_review.CLOSE_REVIEW_RETRY_SECONDS
    )


@pytest.mark.parametrize("verdict", [{"valid": True, "blocking_reasons": []}, None])
def test_claim_finalizing_captures_submitted_verdict(
    verdict: dict[str, Any] | None,
) -> None:
    """Capture-on-arrival is what tells a lost submit from an interrupted one."""
    conn = _RecordingConnection()
    store = TaskCloseReviewStore(cast(Any, SimpleNamespace(transaction=lambda: conn)))

    assert store.claim_finalizing("review", "run", verdict=verdict) is None

    sql, params = conn.executed[0]
    assert "result_payload = %s::jsonb" in sql
    captured = params[0]
    if verdict is None:
        assert captured is None
    else:
        assert json.loads(cast(str, captured)) == {
            "kind": SUBMITTED_VERDICT_KIND,
            "verdict": verdict,
        }


class _RecordingConnection:
    """Transaction stub that records statements without matching a row."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def __enter__(self) -> _RecordingConnection:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def savepoint(self, _name: str) -> SimpleNamespace:
        return SimpleNamespace(rollback=lambda: None, release=lambda: None)

    def execute(self, sql: str, params: tuple[Any, ...]) -> SimpleNamespace:
        self.executed.append((sql, params))
        return SimpleNamespace(fetchone=lambda: None)


class _Store:
    def __init__(self, review: TaskCloseReview) -> None:
        self.review = review
        self.finished_status: str | None = None
        self.delivered = False
        self.writes: list[str] = []

    def list_reconcilable(self) -> list[TaskCloseReview]:
        return [self.review]

    def finish(self, _review_id: str, *, status: str, **kwargs: Any) -> TaskCloseReview:
        self.writes.append("finish")
        self.finished_status = status
        self.review = replace(
            self.review,
            status=cast(TaskCloseReviewStatus, status),
            result_payload=dict(kwargs["result_payload"]),
            error=kwargs.get("error"),
            updated_at=self.review.updated_at + timedelta(seconds=1),
        )
        return self.review

    def finish_run_ended(
        self,
        review_id: str,
        *,
        result_payload: dict[str, Any],
        error: str,
    ) -> TaskCloseReview | None:
        if self.review.status not in ("launching", "running"):
            return None
        return self.finish(review_id, status="error", result_payload=result_payload, error=error)

    def finish_orphaned_finalizing(
        self,
        _review_id: str,
        *,
        expected_updated_at: datetime,
        result_payload: dict[str, Any],
        error: str,
    ) -> TaskCloseReview | None:
        if self.review.status != "finalizing" or self.review.updated_at != expected_updated_at:
            return None
        self.writes.append("finish_orphaned_finalizing")
        self.finished_status = "error"
        self.review = replace(
            self.review,
            status="error",
            result_payload=dict(result_payload),
            error=error,
            updated_at=self.review.updated_at + timedelta(seconds=1),
        )
        return self.review

    def get(self, _review_id: str) -> TaskCloseReview:
        return self.review

    def get_by_run(self, _run_id: str) -> TaskCloseReview:
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
    monkeypatch.setattr(close_review_delivery, "TaskCloseReviewStore", TaskCloseReviewStore)
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


def _install_delivery(
    monkeypatch: pytest.MonkeyPatch,
    *,
    store: _Store,
    run: object | None,
    task: object | None,
) -> None:
    """Let the reconciler drive the real terminal_review_delivery projection."""
    monkeypatch.setattr(close_review_delivery, "TaskCloseReviewStore", lambda _db: store)
    monkeypatch.setattr(
        close_review_delivery,
        "LocalAgentRunManager",
        lambda _db: SimpleNamespace(get=lambda _run_id: run),
    )
    monkeypatch.setattr(
        close_review_delivery,
        "LocalTaskManager",
        lambda _db: SimpleNamespace(get_task=lambda _task_id: task),
    )


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
