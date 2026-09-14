from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from contextvars import ContextVar
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents import terminal_delivery
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.events.wake import WakeDispatcher
from gobby.storage.hub.operation_deadline import (
    current_database_operation_deadline,
    database_operation_deadline,
)
from tests.agents.cleanup_test_support import (
    AcknowledgingCompletionRegistry,
    RecordingDb,
    _handler,
)

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_submitted_delivery_moves_to_owner_loop_and_is_drained() -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()
    trace = ContextVar("terminal_test_trace", default="missing")
    observations: list[tuple[bool, bool, str]] = []

    async def operation() -> None:
        observations.append(
            (
                asyncio.get_running_loop() is loop,
                current_database_operation_deadline() is None,
                trace.get(),
            )
        )
        started.set()
        await release.wait()
        settled.set()

    async def submit() -> None:
        trace.set("caller")
        with database_operation_deadline(timeout_seconds=1) as deadline:
            await terminal_delivery.submit_terminal_delivery("cross-loop", operation)
            assert current_database_operation_deadline() is deadline

    terminal_delivery.configure_terminal_delivery_offload(
        async_offload=asyncio.to_thread, owner_loop=loop
    )
    try:
        await asyncio.to_thread(asyncio.run, submit())
        await asyncio.wait_for(started.wait(), timeout=2)
        assert observations == [(True, True, "caller")]
        assert not settled.is_set()
        draining = asyncio.create_task(terminal_delivery.drain_shielded_terminal_deliveries())
        done, _ = await asyncio.wait({draining}, timeout=0.01)
        assert not done
        release.set()
        await asyncio.wait_for(draining, timeout=2)
        assert settled.is_set()
    finally:
        release.set()
        await terminal_delivery.drain_shielded_terminal_deliveries()
        terminal_delivery.reset_terminal_delivery_offload()


@pytest.mark.asyncio
async def test_submitted_delivery_rejects_closed_admission() -> None:
    invoked = False

    async def operation() -> None:
        nonlocal invoked
        invoked = True

    terminal_delivery.close_terminal_delivery_admission()
    try:
        with pytest.raises(terminal_delivery.TerminalDeliveryAdmissionClosedError):
            await terminal_delivery.submit_terminal_delivery("closed-submit", operation)
        await terminal_delivery.drain_shielded_terminal_deliveries()
        assert not invoked
    finally:
        terminal_delivery.reopen_terminal_delivery_admission()


@pytest.mark.asyncio
async def test_waiting_caller_cancellation_preserves_owned_operation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def operation() -> None:
        started.set()
        await release.wait()
        raise RuntimeError("failure after caller cancellation")

    caller = asyncio.create_task(
        terminal_delivery.run_terminal_delivery("cancelled-caller", operation)
    )
    await started.wait()
    caller.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await caller
    finally:
        release.set()
        await terminal_delivery.drain_shielded_terminal_deliveries()
    assert "Submitted terminal delivery failed for agent cancelled-caller" in caplog.text
    assert "failure after caller cancellation" in caplog.text


@pytest.mark.asyncio
async def test_durable_boundary_reports_pre_boundary_failure() -> None:
    async def operation(_acknowledge: Callable[[str], None]) -> None:
        raise RuntimeError("failed before persistence")

    with pytest.raises(RuntimeError, match="failed before persistence"):
        await terminal_delivery.run_terminal_delivery_until_durable(
            "pre-boundary-failure", operation
        )
    await terminal_delivery.drain_shielded_terminal_deliveries()


@pytest.mark.asyncio
async def test_durable_boundary_reports_owned_operation_cancellation() -> None:
    started = asyncio.Event()

    async def operation(_acknowledge: Callable[[str], None]) -> None:
        started.set()
        await asyncio.Event().wait()

    caller = asyncio.create_task(
        terminal_delivery.run_terminal_delivery_until_durable(
            "pre-boundary-cancellation", operation
        )
    )
    await started.wait()
    assert terminal_delivery.detach_shielded_terminal_deliveries() == ["pre-boundary-cancellation"]
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(caller, timeout=2)


@pytest.mark.asyncio
async def test_durable_boundary_reports_cancellation_before_operation_starts() -> None:
    operation_result: Future[None] = Future()
    operation_result.cancel()
    operation = AsyncMock()

    with (
        patch.object(
            terminal_delivery,
            "submit_terminal_delivery",
            new=AsyncMock(return_value=operation_result),
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await terminal_delivery.run_terminal_delivery_until_durable(
            "cancelled-before-start", operation
        )
    operation.assert_not_awaited()


@pytest.mark.asyncio
async def test_durable_boundary_caller_cancellation_preserves_owned_operation() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()

    async def operation(acknowledge: Callable[[str], None]) -> None:
        started.set()
        await release.wait()
        acknowledge("persisted")
        settled.set()

    caller = asyncio.create_task(
        terminal_delivery.run_terminal_delivery_until_durable("cancelled-boundary", operation)
    )
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    await terminal_delivery.drain_shielded_terminal_deliveries()
    assert settled.is_set()


@pytest.mark.asyncio
async def test_durable_boundary_caller_cancellation_before_cross_loop_admission() -> None:
    owner_loop = asyncio.get_running_loop()
    foreign_loop = asyncio.new_event_loop()
    foreign_loop_ready = threading.Event()
    caller_cancelled = threading.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_settled = asyncio.Event()

    async def operation(acknowledge: Callable[[str], None]) -> None:
        cleanup_started.set()
        acknowledge("persisted")
        await release_cleanup.wait()
        cleanup_settled.set()

    async def cancel_foreign_caller() -> None:
        caller = asyncio.create_task(
            terminal_delivery.run_terminal_delivery_until_durable(
                "cancelled-before-admission", operation
            )
        )
        await asyncio.sleep(0)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        caller_cancelled.set()

    def run_foreign_loop() -> None:
        asyncio.set_event_loop(foreign_loop)
        foreign_loop_ready.set()
        foreign_loop.run_forever()

    thread = threading.Thread(target=run_foreign_loop)
    terminal_delivery.configure_terminal_delivery_offload(
        async_offload=asyncio.to_thread,
        owner_loop=owner_loop,
    )
    try:
        thread.start()
        assert foreign_loop_ready.wait(timeout=2)
        foreign_result = asyncio.run_coroutine_threadsafe(cancel_foreign_caller(), foreign_loop)
        assert caller_cancelled.wait(timeout=2)
        foreign_result.result(timeout=2)
        await asyncio.wait_for(cleanup_started.wait(), timeout=2)
        release_cleanup.set()
        await terminal_delivery.drain_shielded_terminal_deliveries()
        assert cleanup_settled.is_set()
    finally:
        release_cleanup.set()
        terminal_delivery.reset_terminal_delivery_offload()
        foreign_loop.call_soon_threadsafe(foreign_loop.stop)
        thread.join(timeout=2)
        foreign_loop.close()


@pytest.mark.asyncio
async def test_submitted_delivery_reports_background_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def operation() -> None:
        raise RuntimeError("terminal test failure")

    await terminal_delivery.submit_terminal_delivery("failed-submit", operation)
    await terminal_delivery.drain_shielded_terminal_deliveries()
    assert "Submitted terminal delivery failed for agent failed-submit" in caplog.text
    assert "terminal test failure" in caplog.text


async def test_shielded_terminal_delivery_settles_before_cancellation_propagates() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()

    async def operation() -> str:
        started.set()
        await release.wait()
        settled.set()
        return "delivered"

    owner = asyncio.create_task(
        terminal_delivery.shielded_terminal_delivery("run-shielded", operation)
    )
    await started.wait()
    owner.cancel()

    assert owner.cancelling() == 1
    assert not owner.done()
    assert not settled.is_set()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await owner

    assert settled.is_set()
    await terminal_delivery.drain_shielded_terminal_deliveries()


async def test_terminal_delivery_admission_close_blocks_new_scope() -> None:
    invoked = False

    async def operation() -> str:
        nonlocal invoked
        invoked = True
        return "unexpected"

    terminal_delivery.close_terminal_delivery_admission()
    try:
        result = await terminal_delivery.shielded_terminal_delivery("run-closed", operation)
    finally:
        terminal_delivery.reopen_terminal_delivery_admission()

    assert result is None
    assert invoked is False


def test_terminal_delivery_fallback_runs_off_calling_thread() -> None:
    caller_thread = threading.get_ident()

    future = terminal_delivery.submit_terminal_delivery_offload(threading.get_ident)

    assert future.result(timeout=1) != caller_thread


async def test_terminal_delivery_closed_admission_can_raise_explicit_error() -> None:
    invoked = False

    async def operation() -> str:
        nonlocal invoked
        invoked = True
        return "unexpected"

    terminal_delivery.close_terminal_delivery_admission()
    try:
        with pytest.raises(
            terminal_delivery.TerminalDeliveryAdmissionClosedError,
            match="run-closed-strict",
        ):
            await terminal_delivery.shielded_terminal_delivery(
                "run-closed-strict",
                operation,
                raise_if_closed=True,
            )
    finally:
        terminal_delivery.reopen_terminal_delivery_admission()

    assert invoked is False


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "completed"},
        {"status": "error", "error": "failed"},
        {"status": "cancelled"},
    ],
)
async def test_terminal_delivery_injects_run_id_and_removes_only_acknowledged_rows(
    payload: dict[str, object],
) -> None:
    db = RecordingDb()
    registry = AcknowledgingCompletionRegistry({"delivered": True, "retained": False})

    await _handler(db, completion_registry=registry).notify_terminal_completion(
        "run-1",
        result=payload,
        message="Agent terminal",
    )

    assert registry.notifications == [
        (
            "run-1",
            {**payload, "run_id": "run-1"},
            "Agent terminal",
        )
    ]
    assert registry.cleaned == ["run-1"]
    assert db.executed == [
        (
            "DELETE FROM completion_subscribers WHERE completion_id = %s AND session_id = ANY(%s)",
            ("run-1", ["delivered"]),
        )
    ]


async def test_terminal_delivery_without_map_retains_rows_and_cleans_registry() -> None:
    db = RecordingDb()
    registry = AcknowledgingCompletionRegistry(None)

    await _handler(db, completion_registry=registry).notify_terminal_completion(
        "run-1",
        result={"status": "completed"},
        message="Agent completed",
    )

    assert db.executed == []
    assert registry.cleaned == ["run-1"]


class FailingCompletionRegistry(AcknowledgingCompletionRegistry):
    async def notify(
        self,
        completion_id: str,
        result: dict[str, object],
        message: str = "",
        durable_subscriber_count: int = 0,
    ) -> dict[str, bool] | None:
        raise RuntimeError(f"notify failed for {completion_id}: {result!r} {message}")


async def test_terminal_delivery_notify_failure_preserves_registry_state() -> None:
    registry = FailingCompletionRegistry(None)

    delivery = await terminal_delivery.deliver_and_cleanup_terminal_run(
        db=cast("HubDatabase", RecordingDb()),
        completion_registry=cast(Any, registry),
        run_id="run-1",
        result={"status": "completed"},
        message="Agent completed",
        run_db=AsyncMock(),
    )

    assert delivery is None
    assert registry.cleaned == []


async def test_terminal_delivery_orders_remove_and_cleanup_after_awaited_notify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    registry = AcknowledgingCompletionRegistry({"session-1": True}, events)

    def remove_subscribers(**_kwargs: object) -> None:
        events.append("remove")

    async def run_db(
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return func(*args, **kwargs)

    monkeypatch.setattr(
        "gobby.agents.completion_subscribers.remove_agent_completion_subscribers",
        remove_subscribers,
    )

    await terminal_delivery.deliver_and_cleanup_terminal_run(
        db=cast("HubDatabase", RecordingDb()),
        completion_registry=cast(Any, registry),
        run_id="run-1",
        result={"status": "completed"},
        message="Agent completed",
        run_db=run_db,
    )

    assert events == ["notify", "remove", "cleanup"]


async def test_deliver_existing_terminal_run_rereads_and_synthesizes_payload() -> None:
    manager = MagicMock()
    manager.get.return_value = SimpleNamespace(
        id="run-terminal",
        status="cancelled",
        error="cancelled by user",
    )

    async def run_db(
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        return func(*args, **kwargs)

    with patch.object(
        terminal_delivery,
        "deliver_and_cleanup_terminal_run",
        new_callable=AsyncMock,
    ) as deliver:
        delivered = await terminal_delivery.deliver_existing_terminal_run(
            db=MagicMock(),
            agent_run_manager=manager,
            completion_registry=MagicMock(),
            run_id="run-terminal",
            run_db=run_db,
        )

    assert delivered is True
    assert deliver.await_args is not None
    assert deliver.await_args.kwargs["result"] == {
        "status": "cancelled",
        "run_id": "run-terminal",
        "error": "cancelled by user",
    }


async def test_terminal_delivery_projects_persisted_task_close_payload() -> None:
    registry = AcknowledgingCompletionRegistry({"parent": True})
    payload = {
        "event": "task_close_review_completed",
        "review_id": "review",
        "run_id": "run-1",
        "task_id": "task",
        "task_ref": "#42",
        "status": "closed",
        "closed": True,
        "validation_status": "valid",
        "message": "Task closed.",
        "blocking_reasons": [],
        "required_actions": [],
    }
    marked: list[list[str]] = []

    async def run_db(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if func.__name__ == "terminal_review_delivery":
            return payload, "Task closed."
        if func.__name__ == "mark_terminal_review_delivered":
            marked.append(list(args[-1]))
        return None

    await terminal_delivery.deliver_and_cleanup_terminal_run(
        db=cast("HubDatabase", RecordingDb()),
        completion_registry=cast(Any, registry),
        run_id="run-1",
        result={"status": "success"},
        message="generic",
        run_db=run_db,
    )

    assert registry.notifications == [("run-1", payload, "Task closed.")]
    assert marked == [["parent"]]


class DurableDb(RecordingDb):
    """RecordingDb that also serves durable completion_subscribers rows."""

    def __init__(self, subscribers: list[str]) -> None:
        super().__init__()
        self.subscribers = subscribers
        self.queried: list[tuple[str, tuple[object, ...]]] = []

    def fetchall(self, sql: str, params: tuple[object, ...] = ()) -> list[dict[str, str]]:
        self.queried.append((sql, params))
        return [{"session_id": session_id} for session_id in self.subscribers]


class DurableWakeRegistry(AcknowledgingCompletionRegistry):
    """Registry fake exposing the wake_sessions surface the fallback needs."""

    def __init__(
        self,
        delivery: dict[str, bool] | None,
        wake_outcome: dict[str, bool] | None = None,
    ) -> None:
        super().__init__(delivery)
        self.wake_outcome = wake_outcome or {}
        self.woken: list[tuple[str, list[str], dict[str, object], str]] = []

    async def wake_sessions(
        self,
        completion_id: str,
        session_ids: Sequence[str],
        result: dict[str, object],
        message: str = "",
    ) -> dict[str, bool]:
        self.woken.append((completion_id, list(session_ids), result, message))
        return {session_id: self.wake_outcome.get(session_id, True) for session_id in session_ids}


async def test_terminal_delivery_wakes_durable_subscribers_when_registry_is_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = DurableDb(["session-a", "session-b"])
    registry = DurableWakeRegistry(None)

    with caplog.at_level(logging.WARNING, logger="gobby.agents.terminal_delivery"):
        await _handler(db, completion_registry=registry).notify_terminal_completion(
            "run-1",
            result={"status": "completed"},
            message="Agent terminal",
        )

    assert registry.woken == [
        (
            "run-1",
            ["session-a", "session-b"],
            {"status": "completed", "run_id": "run-1"},
            "Agent terminal",
        )
    ]
    assert db.executed == [
        (
            "DELETE FROM completion_subscribers WHERE completion_id = %s AND session_id = ANY(%s)",
            ("run-1", ["session-a", "session-b"]),
        )
    ]
    assert "no in-memory completion subscribers" in caplog.text


async def test_terminal_delivery_wakes_durable_subscriber_after_registry_restart(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fresh registry falls back through durable rows to the real wake dispatcher."""
    db = DurableDb(["session-a"])
    session = SimpleNamespace(
        id="session-a",
        agent_depth=0,
        terminal_context={"tmux_pane": "%7"},
        status="paused",
        turn_count=0,
        session_type="terminal",
        source="codex",
    )
    session_manager = MagicMock()
    session_manager.get.return_value = session
    ism_manager = MagicMock()
    tmux_pane_sender = AsyncMock()

    async def run_inline(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    before_restart = WakeDispatcher(
        session_manager=session_manager,
        ism_manager=ism_manager,
        tmux_pane_sender=tmux_pane_sender,
        run_db=run_inline,
    )
    first_wake = await before_restart.dispatch_live_wake("session-a")
    assert first_wake["delivered"] is True

    after_restart = WakeDispatcher(
        session_manager=session_manager,
        ism_manager=ism_manager,
        tmux_pane_sender=tmux_pane_sender,
        run_db=run_inline,
    )
    restarted_registry = CompletionEventRegistry(wake_callback=after_restart.wake)

    with caplog.at_level(logging.WARNING):
        await _handler(db, completion_registry=restarted_registry).notify_terminal_completion(
            "run-1",
            result={"status": "completed"},
            message="Agent terminal",
        )

    assert tmux_pane_sender.await_count == 2
    assert ism_manager.create_message.call_count == 1
    assert db.executed == [
        (
            "DELETE FROM completion_subscribers WHERE completion_id = %s AND session_id = ANY(%s)",
            ("run-1", ["session-a"]),
        )
    ]
    assert "notify() called for unregistered ID run-1" in caplog.text
    assert "no in-memory completion subscribers" in caplog.text


async def test_terminal_delivery_does_not_rewake_sessions_the_registry_attempted() -> None:
    db = DurableDb(["session-a", "session-b"])
    registry = DurableWakeRegistry({"session-a": True})

    await _handler(db, completion_registry=registry).notify_terminal_completion(
        "run-1",
        result={"status": "completed"},
        message="Agent terminal",
    )

    assert [woken[1] for woken in registry.woken] == [["session-b"]]
    assert db.executed == [
        (
            "DELETE FROM completion_subscribers WHERE completion_id = %s AND session_id = ANY(%s)",
            ("run-1", ["session-a", "session-b"]),
        )
    ]


async def test_terminal_delivery_reports_owed_durable_row_count_to_notify() -> None:
    """The count reaches notify() so the registry can warn about a lost registration.

    The registry cannot see Postgres, so only this caller can distinguish a
    restart-lost registration from a routine duplicate notify.
    """
    db = DurableDb(["session-a", "session-b"])
    registry = DurableWakeRegistry(None)

    await _handler(db, completion_registry=registry).notify_terminal_completion(
        "run-1",
        result={"status": "completed"},
        message="Agent terminal",
    )

    assert registry.durable_subscriber_counts == [2]


async def test_terminal_delivery_reports_zero_owed_rows_when_none_are_durable() -> None:
    """No durable rows means no state was lost, so notify() must not be told otherwise."""
    db = RecordingDb()
    registry = DurableWakeRegistry({"session-1": True})

    await _handler(db, completion_registry=registry).notify_terminal_completion(
        "run-1",
        result={"status": "completed"},
        message="Agent terminal",
    )

    assert registry.durable_subscriber_counts == [0]
    assert registry.woken == []


async def test_terminal_delivery_retains_rows_for_undelivered_durable_sessions() -> None:
    db = DurableDb(["session-a", "session-b"])
    registry = DurableWakeRegistry(None, wake_outcome={"session-b": False})

    await _handler(db, completion_registry=registry).notify_terminal_completion(
        "run-1",
        result={"status": "completed"},
        message="Agent terminal",
    )

    assert db.executed == [
        (
            "DELETE FROM completion_subscribers WHERE completion_id = %s AND session_id = ANY(%s)",
            ("run-1", ["session-a"]),
        )
    ]


@pytest.mark.asyncio
async def test_concurrent_terminal_delivery_coalesces_acknowledged_wake(
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = DurableDb(["session-a"])
    wake_started = asyncio.Event()
    release_wake = asyncio.Event()
    wake_calls = 0

    async def wake(
        _session_id: str,
        _message: str,
        _payload: dict[str, object],
    ) -> dict[str, bool]:
        nonlocal wake_calls
        wake_calls += 1
        wake_started.set()
        await release_wake.wait()
        return {"ism_persisted": True}

    registry = CompletionEventRegistry(wake_callback=wake)
    registry.register("run-1", ["session-a"])
    handler = _handler(db, completion_registry=registry)
    first = asyncio.create_task(
        handler.notify_terminal_completion(
            "run-1", result={"status": "completed"}, message="Agent terminal"
        )
    )
    await asyncio.wait_for(wake_started.wait(), timeout=2)
    second = asyncio.create_task(
        handler.notify_terminal_completion(
            "run-1", result={"status": "completed"}, message="Agent terminal"
        )
    )
    await asyncio.sleep(0)

    assert wake_calls == 1
    release_wake.set()
    await asyncio.gather(first, second)
    assert db.executed == [
        (
            "DELETE FROM completion_subscribers WHERE completion_id = %s AND session_id = ANY(%s)",
            ("run-1", ["session-a"]),
        )
    ]
    assert "went terminal with no in-memory completion subscribers" not in caplog.text


@pytest.mark.asyncio
async def test_concurrent_terminal_delivery_honors_waiter_cancellation() -> None:
    db = DurableDb(["session-a"])
    wake_started = asyncio.Event()
    release_wake = asyncio.Event()
    wake_calls = 0

    async def wake(
        _session_id: str,
        _message: str,
        _payload: dict[str, object],
    ) -> dict[str, bool]:
        nonlocal wake_calls
        wake_calls += 1
        wake_started.set()
        await release_wake.wait()
        return {"ism_persisted": True}

    registry = CompletionEventRegistry(wake_callback=wake)
    registry.register("run-1", ["session-a"])
    handler = _handler(db, completion_registry=registry)
    first = asyncio.create_task(
        handler.notify_terminal_completion(
            "run-1", result={"status": "completed"}, message="Agent terminal"
        )
    )
    await asyncio.wait_for(wake_started.wait(), timeout=2)
    second = asyncio.create_task(
        handler.notify_terminal_completion(
            "run-1", result={"status": "completed"}, message="Agent terminal"
        )
    )
    await asyncio.sleep(0)

    first.cancel()
    second.cancel()
    release_wake.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)

    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
    assert wake_calls == 1


@pytest.mark.asyncio
async def test_concurrent_terminal_delivery_retries_unacknowledged_wake() -> None:
    db = DurableDb(["session-a"])
    wake_started = asyncio.Event()
    release_wake = asyncio.Event()
    wake_calls = 0

    async def wake(
        _session_id: str,
        _message: str,
        _payload: dict[str, object],
    ) -> dict[str, bool] | None:
        nonlocal wake_calls
        wake_calls += 1
        if wake_calls == 1:
            wake_started.set()
            await release_wake.wait()
            return None
        return {"ism_persisted": True}

    registry = CompletionEventRegistry(wake_callback=wake)
    registry.register("run-1", ["session-a"])
    handler = _handler(db, completion_registry=registry)
    first = asyncio.create_task(
        handler.notify_terminal_completion(
            "run-1", result={"status": "completed"}, message="Agent terminal"
        )
    )
    await asyncio.wait_for(wake_started.wait(), timeout=2)
    second = asyncio.create_task(
        handler.notify_terminal_completion(
            "run-1", result={"status": "completed"}, message="Agent terminal"
        )
    )
    release_wake.set()
    await asyncio.gather(first, second)

    assert wake_calls == 2
    assert db.executed == [
        (
            "DELETE FROM completion_subscribers WHERE completion_id = %s AND session_id = ANY(%s)",
            ("run-1", ["session-a"]),
        )
    ]


@pytest.mark.asyncio
async def test_concurrent_terminal_delivery_retries_failed_row_cleanup() -> None:
    db = DurableDb(["session-a"])
    wake_started = asyncio.Event()
    release_wake = asyncio.Event()
    removal_attempts = 0
    wake_calls = 0

    async def wake(
        _session_id: str,
        _message: str,
        _payload: dict[str, object],
    ) -> dict[str, bool]:
        nonlocal wake_calls
        wake_calls += 1
        if wake_calls == 1:
            wake_started.set()
            await release_wake.wait()
        return {"ism_persisted": True}

    async def run_db(
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        nonlocal removal_attempts
        if func.__name__ == "terminal_review_delivery":
            return None
        if func.__name__ == "remove_delivered_subscribers":
            removal_attempts += 1
            if removal_attempts == 1:
                raise RuntimeError("injected subscriber cleanup failure")
        return func(*args, **kwargs)

    registry = CompletionEventRegistry(wake_callback=wake)
    registry.register("run-1", ["session-a"])
    handler = _handler(db, run_db, completion_registry=registry)
    first = asyncio.create_task(
        handler.notify_terminal_completion(
            "run-1", result={"status": "completed"}, message="Agent terminal"
        )
    )
    await asyncio.wait_for(wake_started.wait(), timeout=2)
    second = asyncio.create_task(
        handler.notify_terminal_completion(
            "run-1", result={"status": "completed"}, message="Agent terminal"
        )
    )
    release_wake.set()
    await asyncio.gather(first, second)

    assert wake_calls == 2
    assert removal_attempts == 2
    assert db.executed == [
        (
            "DELETE FROM completion_subscribers WHERE completion_id = %s AND session_id = ANY(%s)",
            ("run-1", ["session-a"]),
        )
    ]
