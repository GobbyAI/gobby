"""Multi-recipient wake orchestration for the native terminal host."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any

from gobby.events.live_wake import wake_debounced_result, wake_failure
from gobby.events.wake import NativeWakeTarget

if TYPE_CHECKING:
    from gobby.events.wake import WakeDispatcher

logger = logging.getLogger(__name__)


async def dispatch_live_wakes(
    dispatcher: WakeDispatcher,
    session_ids: list[str],
    *,
    priority: str,
) -> list[dict[str, Any]]:
    """Use one host exchange for native targets and existing paths for the rest."""
    if not session_ids:
        return []

    locks: list[asyncio.Lock] = []
    for session_id in sorted(set(session_ids)):
        lock = dispatcher._live_wake_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            dispatcher._live_wake_locks[session_id] = lock
        locks.append(lock)

    async with AsyncExitStack() as stack:
        for lock in locks:
            await stack.enter_async_context(lock)

        sessions: dict[str, Any] = {}
        results: dict[str, dict[str, Any]] = {}
        for session_id in session_ids:

            def read_session(current_id: str = session_id) -> Any | None:
                with dispatcher._session_manager.db.bounded_transaction():
                    return dispatcher._session_manager.get(current_id)

            try:
                sessions[session_id] = await dispatcher._run_db(read_session)
            except Exception as exc:
                logger.warning("Session lookup failed before batch wake", exc_info=True)
                results[session_id] = wake_failure(
                    session_id,
                    method=None,
                    error_code="session_lookup_failed",
                    error_message=str(exc) or type(exc).__name__,
                )

        fallback: list[tuple[str, Any]] = []
        native_targets: list[NativeWakeTarget] = []
        native_sessions: dict[str, Any] = {}
        for session_id in session_ids:
            if session_id in results:
                continue
            session = sessions[session_id]
            if session is None:
                results[session_id] = wake_failure(
                    session_id,
                    method=None,
                    error_code="session_not_found",
                    error_message=f"Session {session_id} not found",
                )
                continue
            if getattr(session, "status", None) == "active" and priority != "urgent":
                fallback.append((session_id, session))
                continue
            if (
                getattr(session, "agent_depth", 0) != 0
                or dispatcher._terminal_manager is None
                or dispatcher._tmux_sender is None
                or dispatcher._native_batch_sender is None
            ):
                fallback.append((session_id, session))
                continue
            terminal_route = await dispatcher._terminal_route_for_session(session)
            terminal = terminal_route.managed_terminal
            if terminal is None or getattr(terminal, "backend", None) != "native":
                fallback.append((session_id, session))
                continue
            if not dispatcher._should_send_live_wake(session_id, session):
                results[session_id] = wake_debounced_result(session_id, method="terminal")
                continue
            current, state_failure = await dispatcher._preflight_live_side_effect(
                session_id, priority=priority
            )
            if state_failure is not None:
                results[session_id] = state_failure
                continue
            if current is not None:
                session = current
            blocked = await dispatcher._composer_blocks_wake(
                session_id, session, terminal, method="terminal"
            )
            if blocked is not None:
                results[session_id] = blocked
                continue
            native_sessions[session_id] = session
            native_targets.append(
                NativeWakeTarget(
                    session_id=session_id,
                    terminal_id=str(terminal.id),
                    cli_source=getattr(session, "source", None),
                )
            )

        async with asyncio.TaskGroup() as group:
            native_task = (
                group.create_task(_send_native(dispatcher, native_targets))
                if native_targets
                else None
            )
            fallback_tasks = {
                session_id: group.create_task(
                    _send_fallback(dispatcher, session_id, session, priority=priority)
                )
                for session_id, session in fallback
            }
        if native_task is not None:
            for result in native_task.result():
                session_id = str(result["session_id"])
                if result.get("delivered") is True:
                    dispatcher._record_live_wake(session_id, native_sessions[session_id])
                results[session_id] = result
        for session_id, task in fallback_tasks.items():
            results[session_id] = task.result()
        return [results[session_id] for session_id in session_ids]


async def _send_native(
    dispatcher: WakeDispatcher,
    targets: list[NativeWakeTarget],
) -> list[dict[str, Any]]:
    sender = dispatcher._native_batch_sender
    assert sender is not None
    try:
        raw_results = await sender(targets)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Native terminal wake batch failed", exc_info=True)
        detail = str(exc) or type(exc).__name__
        raw_results = [
            wake_failure(
                target.session_id,
                method="terminal",
                error_code="native_wake_batch_failed",
                error_message=detail,
            )
            for target in targets
        ]

    by_session = {
        str(result.get("session_id")): result
        for result in raw_results
        if isinstance(result, dict) and result.get("session_id") is not None
    }
    normalized: list[dict[str, Any]] = []
    for target in targets:
        result = by_session.get(target.session_id)
        if result is None:
            result = wake_failure(
                target.session_id,
                method="terminal",
                error_code="native_wake_result_missing",
                error_message="Native wake batch returned no result for the recipient",
            )
        normalized.append(result)
    return normalized


async def _send_fallback(
    dispatcher: WakeDispatcher,
    session_id: str,
    session: Any,
    *,
    priority: str,
) -> dict[str, Any]:
    try:
        return await dispatcher._dispatch_live_wake_unlocked(
            session_id,
            session=session,
            priority=priority,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("Wake dispatch failed for session %s", session_id, exc_info=True)
        return wake_failure(
            session_id,
            method=None,
            error_code="wake_dispatch_failed",
            error_message=str(exc) or type(exc).__name__,
        )
