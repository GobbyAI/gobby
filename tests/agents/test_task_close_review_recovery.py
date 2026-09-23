"""Daemon-start reconciliation tests for durable task-close reviews."""

from __future__ import annotations

import json
from contextlib import ExitStack
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
    FINALIZING_ORPHAN_GRACE_SECONDS,
    SUBMITTED_VERDICT_KIND,
    TaskCloseReview,
    TaskCloseReviewStatus,
    TaskCloseReviewStore,
    finalizing_in_process,
)

pytestmark = pytest.mark.unit

_CLOSED_TASK = SimpleNamespace(
    id="task", commits=["abc123"], closed_at=datetime(2026, 9, 8, tzinfo=UTC)
)
_THIS_MACHINE = "machine-under-test"
_NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


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


async def test_queued_review_rehydrates_wait_and_promotes_on_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store(_review(status="queued", run_id="queued-run"))
    subscribers = _Subscribers()
    wake = AsyncMock(return_value={"ism_persisted": True})
    _install(
        monkeypatch,
        store=store,
        run=SimpleNamespace(id="queued-run", status="queued"),
        subscribers=subscribers,
    )
    promoter = AsyncMock(return_value=["review"])
    registry = SimpleNamespace(
        get_private_callback=lambda name: promoter if name == "promote_close_reviews" else None
    )
    manager = SimpleNamespace(get_registry=lambda name: registry if name == "gobby-tasks" else None)
    runner = _runner(wake)
    runner.http_server = SimpleNamespace(_internal_manager=manager)

    recovered = await lifecycle_agents._reconcile_task_close_reviews_on_startup(runner)

    assert recovered == 1
    assert subscribers.added == [("queued-run", ["parent"])]
    promoter.assert_awaited_once_with()
    wake.assert_not_awaited()


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
        terminal_payload="Task-close reviewer exceeded its durable deadline.",
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
        message = "reviewer ended without a verdict"
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
@pytest.mark.parametrize(
    "machine_id,age_seconds,held_in_process,expected_status,expected_writes",
    [
        pytest.param(
            _THIS_MACHINE,
            FINALIZING_ORPHAN_GRACE_SECONDS + 1,
            False,
            "error",
            ["finish_orphaned_finalizing"],
            id="proven-orphan",
        ),
        pytest.param(
            _THIS_MACHINE,
            FINALIZING_ORPHAN_GRACE_SECONDS + 1,
            True,
            "finalizing",
            [],
            id="held-by-a-live-submission",
        ),
        pytest.param(
            _THIS_MACHINE,
            FINALIZING_ORPHAN_GRACE_SECONDS - 1,
            False,
            "finalizing",
            [],
            id="within-grace",
        ),
        pytest.param(
            "another-machine",
            FINALIZING_ORPHAN_GRACE_SECONDS + 1,
            False,
            "finalizing",
            [],
            id="foreign-machine",
        ),
    ],
)
async def test_periodic_tick_sweeps_only_a_proven_orphaned_finalizing_review(
    monkeypatch: pytest.MonkeyPatch,
    machine_id: str,
    age_seconds: int,
    held_in_process: bool,
    expected_status: str,
    expected_writes: list[str],
) -> None:
    """A running daemon recovers a stranded review only on all three proofs.

    `finalizing` is held across `commit_close`, so the status alone never
    authorizes a write: a live `submit_close_review` would be contradicted. The
    in-process set, the machine scope, and the grace bound on `updated_at` are
    jointly what make the row provably abandoned without a restart.
    """
    store = _Store(
        replace(
            _review(status="finalizing", run_id="run"),
            result_payload={"kind": SUBMITTED_VERDICT_KIND, "verdict": {"valid": True}},
            updated_at=_NOW - timedelta(seconds=age_seconds),
        )
    )
    run = SimpleNamespace(id="run", status="running", machine_id=machine_id)
    wake = AsyncMock(return_value={"ism_persisted": True})
    _install(monkeypatch, store=store, run=run, subscribers=_Subscribers())
    _install_delivery(monkeypatch, store=store, run=run, task=None)
    monkeypatch.setattr(lifecycle_agents, "utc_now", lambda: _NOW)
    monkeypatch.setattr(lifecycle_agents, "get_machine_id", lambda: _THIS_MACHINE)

    with ExitStack() as stack:
        if held_in_process:
            stack.enter_context(finalizing_in_process(store.review.id))
        await lifecycle_agents._reconcile_task_close_reviews(_runner(wake), startup=False)

    assert store.review.status == expected_status
    assert store.writes == expected_writes
    if not expected_writes:
        # An unproven row keeps its captured verdict and its active lock; the
        # submission that owns it is the only writer allowed to finish it.
        assert store.review.result_payload == {
            "kind": SUBMITTED_VERDICT_KIND,
            "verdict": {"valid": True},
        }
        assert store.review.active is True
        assert store.delivered is False
        wake.assert_not_awaited()
        return

    # The sweep releases uq_task_close_reviews_active_task and tells the caller
    # to close again rather than replaying the dead verdict.
    assert store.review.active is False
    assert store.delivered is True
    payload = wake.call_args.args[2]
    assert payload["closed"] is False
    assert payload["error_class"] == "retryable_infrastructure"
    assert "verdict" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("visible_to_reread", [True, False], ids=["re-proof", "compare-and-swap"])
async def test_a_reviving_submission_wins_the_periodic_sweep_race(
    monkeypatch: pytest.MonkeyPatch,
    visible_to_reread: bool,
) -> None:
    """A submission that revives the row mid-sweep is never contradicted.

    The proof is necessarily read outside the write, so two guards cover the
    gap. A revival visible to the sweep's re-read fails the proof again; one
    that lands after it fails the `updated_at` compare-and-swap in
    `finish_orphaned_finalizing`. Either way the reconciler writes nothing.
    """
    stale = replace(
        _review(status="finalizing", run_id="run"),
        updated_at=_NOW - timedelta(seconds=FINALIZING_ORPHAN_GRACE_SECONDS + 1),
    )
    store = _Store(stale)
    run = SimpleNamespace(id="run", status="running", machine_id=_THIS_MACHINE)
    wake = AsyncMock(return_value={"ism_persisted": True})
    _install(monkeypatch, store=store, run=run, subscribers=_Subscribers())
    _install_delivery(monkeypatch, store=store, run=run, task=None)
    monkeypatch.setattr(lifecycle_agents, "utc_now", lambda: _NOW)
    monkeypatch.setattr(lifecycle_agents, "get_machine_id", lambda: _THIS_MACHINE)

    revived = replace(stale, updated_at=_NOW)

    def get_and_revive(_review_id: str) -> TaskCloseReview:
        # Stand in for a concurrent claim landing during the sweep. The row the
        # re-read observes is what decides which of the two guards catches it.
        store.review = revived
        return revived if visible_to_reread else stale

    monkeypatch.setattr(store, "get", get_and_revive)

    await lifecycle_agents._reconcile_task_close_reviews(_runner(wake), startup=False)

    assert store.writes == []
    assert store.review is revived
    assert store.review.status == "finalizing"
    wake.assert_not_awaited()
    # Which guard caught it: the re-proof refuses to attempt the write at all,
    # while a revival it cannot see is rejected by the compare-and-swap.
    assert store.orphan_sweep_attempts == ([] if visible_to_reread else [stale.updated_at])


@pytest.mark.asyncio
async def test_daemon_stop_parked_reviewer_retries_on_a_short_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reviewer parked by the daemon's own stop waits seconds, not 900s."""
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
        self.orphan_sweep_attempts: list[datetime] = []

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
        self.orphan_sweep_attempts.append(expected_updated_at)
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
