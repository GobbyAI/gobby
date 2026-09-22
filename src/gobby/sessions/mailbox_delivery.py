"""Live-wake orchestration for committed mailbox messages."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from gobby.storage.inter_session_messages import InterSessionMessage
    from gobby.storage.sessions import SessionManager

logger = logging.getLogger(__name__)


class WakeDispatcherProtocol(Protocol):
    async def dispatch_live_wake(
        self, session_id: str, *, priority: str = "normal"
    ) -> dict[str, Any]: ...


def _normalize_wake_result(
    session_manager: SessionManager,
    session_id: str,
    result: Any,
) -> dict[str, Any]:
    if isinstance(result, dict):
        normalized = dict(result)
    elif isinstance(result, BaseException):
        detail = str(result) or type(result).__name__
        normalized = {
            "session_id": session_id,
            "delivered": False,
            "method": None,
            "error": detail,
            "error_code": "wake_dispatch_failed",
            "error_message": detail,
        }
    else:
        normalized = {"session_id": session_id, "delivered": False, "method": None}
    session = session_manager.get(session_id)
    normalized.setdefault("session_id", session_id)
    normalized.setdefault("session_status", getattr(session, "status", None))
    return normalized


async def _wake_one(
    dispatcher: WakeDispatcherProtocol | None,
    session_id: str,
    *,
    priority: str,
) -> dict[str, Any]:
    if dispatcher is None:
        return {
            "session_id": session_id,
            "delivered": False,
            "method": None,
            "error": "wake_dispatcher_unavailable",
            "error_code": "wake_dispatcher_unavailable",
            "error_message": "Wake dispatcher is unavailable",
        }
    try:
        result = await dispatcher.dispatch_live_wake(session_id, priority=priority)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Mailbox wake dispatch failed for session %s: %s",
            session_id,
            exc,
            exc_info=True,
        )
        return {
            "session_id": session_id,
            "delivered": False,
            "method": None,
            "error": str(exc),
            "error_code": "wake_dispatch_failed",
            "error_message": str(exc),
        }
    if isinstance(result, dict):
        return result
    return {"session_id": session_id, "delivered": False, "method": None}


async def _wake_many(
    dispatcher: WakeDispatcherProtocol | None,
    session_manager: SessionManager,
    session_ids: list[str],
    *,
    priority: str,
) -> list[dict[str, Any]]:
    batch_wake = getattr(dispatcher, "dispatch_live_wakes", None)
    if len(session_ids) > 1 and callable(batch_wake):
        try:
            raw_results = await batch_wake(session_ids, priority=priority)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Mailbox batch wake dispatch failed: %s", exc, exc_info=True)
            raw_results = [
                {
                    "session_id": session_id,
                    "delivered": False,
                    "method": None,
                    "error": str(exc),
                    "error_code": "wake_dispatch_failed",
                    "error_message": str(exc),
                }
                for session_id in session_ids
            ]
        if not isinstance(raw_results, list):
            raw_results = []
        by_session = {
            str(result.get("session_id")): result
            for result in raw_results
            if isinstance(result, dict) and result.get("session_id") is not None
        }
        return [
            _normalize_wake_result(
                session_manager,
                session_id,
                by_session.get(
                    session_id,
                    {
                        "session_id": session_id,
                        "delivered": False,
                        "method": None,
                        "error_code": "wake_result_missing",
                        "error_message": "Wake dispatcher returned no result",
                    },
                ),
            )
            for session_id in session_ids
        ]

    async with asyncio.TaskGroup() as group:
        wakes = [
            group.create_task(_wake_one(dispatcher, session_id, priority=priority))
            for session_id in session_ids
        ]
    return [
        _normalize_wake_result(session_manager, session_id, wake.result())
        for session_id, wake in zip(session_ids, wakes, strict=True)
    ]


async def dispatch_mailbox_wakes(
    dispatcher: WakeDispatcherProtocol | None,
    session_manager: SessionManager,
    messages: list[InterSessionMessage],
    session_ids: list[str],
    *,
    priority: str,
) -> list[dict[str, Any]]:
    """Dispatch and correlate one live-wake outcome per committed message."""
    if len(messages) != len(session_ids):
        raise RuntimeError("Committed mailbox messages do not match resolved recipients")
    outcomes = await _wake_many(
        dispatcher,
        session_manager,
        session_ids,
        priority=priority,
    )
    correlated: list[dict[str, Any]] = []
    for message, session_id, outcome in zip(messages, session_ids, outcomes, strict=True):
        item = {**outcome, "message_id": message.id}
        decline_reason = item.get("decline_reason")
        if isinstance(decline_reason, str) and decline_reason:
            level = logging.DEBUG if decline_reason == "session_active" else logging.INFO
            logger.log(
                level,
                "mailbox wake declined for session %s message %s: %s",
                session_id,
                message.id,
                decline_reason,
            )
        correlated.append(item)
    return correlated
