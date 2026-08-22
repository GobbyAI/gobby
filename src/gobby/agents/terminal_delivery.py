from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, overload

from gobby.storage.agents import TERMINAL_AGENT_RUN_STATUSES

if TYPE_CHECKING:
    from gobby.events.completion_registry import CompletionEventRegistry
    from gobby.storage.agents import AgentRun
    from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


class _TerminalRunStorage(Protocol):
    db: HubDatabase

    def get(self, run_id: str) -> Any | None: ...


_terminal_delivery_admission_open = True
_in_flight_terminal_deliveries: dict[asyncio.Task[Any], str] = {}


async def _default_terminal_delivery_offload[T](
    callback: Callable[..., T],
    *args: object,
    **kwargs: object,
) -> T:
    return await asyncio.to_thread(callback, *args, **kwargs)


_terminal_delivery_offload: Callable[..., Awaitable[Any]] = _default_terminal_delivery_offload
_terminal_delivery_submit: Callable[..., Future[Any]] | None = None
_terminal_delivery_fallback_executor = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="gobby-terminal-delivery",
)


def configure_terminal_delivery_offload(
    *,
    async_offload: Callable[..., Awaitable[Any]],
    sync_submit: Callable[..., Future[Any]] | None = None,
) -> None:
    """Route terminal storage work through the owned daemon executor."""
    global _terminal_delivery_offload, _terminal_delivery_submit
    _terminal_delivery_offload = async_offload
    _terminal_delivery_submit = sync_submit


def reset_terminal_delivery_offload() -> None:
    """Restore default executor seams for tests and pre-daemon callers."""
    global _terminal_delivery_offload, _terminal_delivery_submit
    _terminal_delivery_offload = _default_terminal_delivery_offload
    _terminal_delivery_submit = None


async def run_terminal_delivery_offload[T](
    callback: Callable[..., T],
    *args: object,
    **kwargs: object,
) -> T:
    """Run blocking terminal storage work through the configured async seam."""
    return cast(T, await _terminal_delivery_offload(callback, *args, **kwargs))


def submit_terminal_delivery_offload[T](
    callback: Callable[..., T],
    *args: object,
    **kwargs: object,
) -> Future[T]:
    """Submit terminal storage work from a synchronous hook thread."""
    if _terminal_delivery_submit is not None:
        return cast(Future[T], _terminal_delivery_submit(callback, *args, **kwargs))
    return _terminal_delivery_fallback_executor.submit(callback, *args, **kwargs)


def close_terminal_delivery_admission() -> None:
    """Prevent new terminal transition-and-delivery scopes from starting."""
    global _terminal_delivery_admission_open
    _terminal_delivery_admission_open = False


def reopen_terminal_delivery_admission() -> None:
    """Open terminal delivery admission for a newly-owned daemon lifecycle."""
    if _in_flight_terminal_deliveries:
        raise RuntimeError("Cannot reopen terminal delivery admission with work in flight")
    global _terminal_delivery_admission_open
    _terminal_delivery_admission_open = True


class TerminalDeliveryAdmissionClosedError(RuntimeError):
    """Raised when terminal delivery cannot be admitted during shutdown."""


@overload
async def shielded_terminal_delivery[T](
    run_id: str,
    operation: Callable[[], Coroutine[Any, Any, T]],
    *,
    raise_if_closed: Literal[True],
) -> T: ...


@overload
async def shielded_terminal_delivery[T](
    run_id: str,
    operation: Callable[[], Coroutine[Any, Any, T]],
    *,
    raise_if_closed: bool = False,
) -> T | None: ...


async def shielded_terminal_delivery[T](
    run_id: str,
    operation: Callable[[], Coroutine[Any, Any, T]],
    *,
    raise_if_closed: bool = False,
) -> T | None:
    """Settle one owned terminal transition-and-delivery operation under cancellation."""
    if not _terminal_delivery_admission_open:
        logger.info("Terminal delivery admission is closed for agent %s", run_id)
        if raise_if_closed:
            raise TerminalDeliveryAdmissionClosedError(
                f"Terminal delivery admission is closed for agent {run_id}"
            )
        return None

    owned: asyncio.Task[T] = asyncio.create_task(
        operation(),
        name=f"terminal-delivery:{run_id}",
    )
    _in_flight_terminal_deliveries[owned] = run_id
    owned.add_done_callback(lambda task: _in_flight_terminal_deliveries.pop(task, None))

    cancellation: asyncio.CancelledError | None = None
    while not owned.done():
        try:
            await asyncio.shield(owned)
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc

    if cancellation is not None:
        try:
            owned.result()
        except BaseException:
            logger.warning(
                "Terminal delivery failed while caller cancellation settled for agent %s",
                run_id,
                exc_info=True,
            )
        raise cancellation
    return owned.result()


async def drain_shielded_terminal_deliveries() -> None:
    """Await tracked terminal delivery scopes until the set is stably empty."""
    while _in_flight_terminal_deliveries:
        snapshot = tuple(_in_flight_terminal_deliveries)
        await asyncio.gather(
            *(asyncio.shield(task) for task in snapshot),
            return_exceptions=True,
        )


def detach_shielded_terminal_deliveries() -> list[str]:
    """Cancel overdue delivery work and retain durable rows for next-boot recovery."""
    detached = list(_in_flight_terminal_deliveries.values())
    for task in tuple(_in_flight_terminal_deliveries):
        task.cancel()
        _in_flight_terminal_deliveries.pop(task, None)
    return detached


async def deliver_and_cleanup_terminal_run(
    *,
    db: HubDatabase,
    completion_registry: CompletionEventRegistry | None,
    run_id: str,
    result: dict[str, Any] | None,
    message: str,
    run_db: Callable[..., Awaitable[Any]],
) -> dict[str, bool] | None:
    """Deliver a terminal result, remove acknowledged rows, then evict registry state."""
    from gobby.tasks.close_review_delivery import terminal_review_delivery

    try:
        review_delivery = await run_db(terminal_review_delivery, db, run_id)
    except Exception:
        logger.warning("Failed to resolve task-close review delivery for %s", run_id, exc_info=True)
        review_delivery = None
    if isinstance(review_delivery, tuple) and len(review_delivery) == 2:
        result, message = review_delivery
    if completion_registry is None:
        return None

    delivery: dict[str, bool] | None = None
    notification_succeeded = False
    if result is not None:
        payload = result if "run_id" in result else {**result, "run_id": run_id}
        try:
            delivery = await completion_registry.notify(run_id, result=payload, message=message)
            notification_succeeded = True
        except Exception:
            logger.warning("Failed to notify completion for %s", run_id, exc_info=True)

    delivered_session_ids = (
        [session_id for session_id, delivered in delivery.items() if delivered]
        if isinstance(delivery, dict)
        else []
    )
    try:
        if delivered_session_ids:
            from gobby.agents.completion_subscribers import (
                remove_agent_completion_subscribers,
            )

            def remove_delivered_subscribers() -> None:
                with db.bounded_transaction():
                    remove_agent_completion_subscribers(
                        db=db,
                        run_id=run_id,
                        session_ids=delivered_session_ids,
                    )

            await run_db(remove_delivered_subscribers)
    except Exception:
        logger.warning(
            "Failed to remove delivered completion subscribers for agent %s",
            run_id,
            exc_info=True,
        )
    if delivered_session_ids and result is not None:
        try:
            from gobby.tasks.close_review_delivery import mark_terminal_review_delivered

            await run_db(
                mark_terminal_review_delivered,
                db,
                result,
                delivered_session_ids,
            )
        except Exception:
            logger.warning(
                "Failed to mark task-close review delivery for agent %s",
                run_id,
                exc_info=True,
            )
    if notification_succeeded:
        completion_registry.cleanup(run_id)
    return delivery


async def deliver_existing_terminal_run_unshielded(
    *,
    db: HubDatabase,
    agent_run_manager: _TerminalRunStorage,
    completion_registry: CompletionEventRegistry | None,
    run_id: str,
    run_db: Callable[..., Awaitable[Any]],
    message: str | None = None,
) -> bool:
    """Re-read one terminal run under bounds and deliver it without opening a scope."""

    def read_terminal_run() -> AgentRun | None:
        with db.bounded_transaction():
            return agent_run_manager.get(run_id)

    db_run = await run_db(read_terminal_run)
    if db_run is None or db_run.status not in TERMINAL_AGENT_RUN_STATUSES:
        return False
    result = {
        "status": db_run.status,
        "run_id": db_run.id,
        "error": db_run.error,
    }
    await deliver_and_cleanup_terminal_run(
        db=db,
        completion_registry=completion_registry,
        run_id=run_id,
        result=result,
        message=message or f"Agent {run_id} {db_run.status}",
        run_db=run_db,
    )
    return True


deliver_existing_terminal_run_in_scope = deliver_existing_terminal_run_unshielded


async def deliver_existing_terminal_run(
    *,
    db: HubDatabase,
    agent_run_manager: _TerminalRunStorage,
    completion_registry: CompletionEventRegistry | None,
    run_id: str,
    run_db: Callable[..., Awaitable[Any]],
    message: str | None = None,
) -> bool:
    """Shield a terminal re-read and acknowledged delivery from caller cancellation."""

    async def operation() -> bool:
        return await deliver_existing_terminal_run_unshielded(
            db=db,
            agent_run_manager=agent_run_manager,
            completion_registry=completion_registry,
            run_id=run_id,
            run_db=run_db,
            message=message,
        )

    return bool(await shielded_terminal_delivery(run_id, operation))
