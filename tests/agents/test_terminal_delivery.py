from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents import terminal_delivery
from tests.agents.cleanup_test_support import (
    AcknowledgingCompletionRegistry,
    RecordingDb,
    _handler,
)

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit


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
