"""Durable completion-notification persistence."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from gobby.storage.inter_session_messages import InterSessionMessageManager

logger = logging.getLogger(__name__)

RunDb = Callable[..., Awaitable[Any]]


def _completion_id(metadata: dict[str, Any]) -> str | None:
    value = metadata.get("completion_id") or metadata.get("run_id") or metadata.get("execution_id")
    return str(value) if value else None


def _notification_exists(
    manager: InterSessionMessageManager,
    session_id: str,
    message_type: str,
    completion_id: str,
) -> bool:
    has_notification = getattr(type(manager), "has_completion_notification", None)
    if callable(has_notification):
        try:
            return bool(has_notification(manager, session_id, message_type, completion_id))
        except Exception:
            logger.debug(
                "Could not query existing completion notification for %s",
                session_id,
                exc_info=True,
            )

    list_messages = getattr(manager, "list_messages", None)
    if not callable(list_messages):
        return False
    try:
        messages = list_messages(
            session_id,
            direction="inbox",
            message_type=message_type,
            limit=100,
        )
    except Exception:
        logger.debug(
            "Could not query existing completion notifications for %s",
            session_id,
            exc_info=True,
        )
        return False

    for existing in messages:
        metadata_json = getattr(existing, "metadata_json", None)
        if not metadata_json:
            continue
        try:
            metadata = json.loads(metadata_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if _completion_id(metadata) == completion_id:
            return True
    return False


async def persist_completion_notification(
    manager: InterSessionMessageManager,
    run_db: RunDb,
    session_id: str,
    message: str,
    result: dict[str, Any],
) -> bool:
    """Persist one idempotent completion notification."""

    def persist_notification() -> bool:
        content = str(
            result.get("signoff_message")
            or result.get("continuation_prompt")
            or message
            or "Completion available"
        )
        from_session = str(result.get("from_session_id") or session_id)
        message_type = str(result.get("message_type") or "completion_notification")
        metadata = {**result, "completion_message": message}
        completion_id = _completion_id(metadata)
        if completion_id and "completion_id" not in metadata:
            metadata["completion_id"] = completion_id
        priority = str(result.get("priority") or "normal")
        with manager.db.bounded_transaction():
            if completion_id and _notification_exists(
                manager,
                session_id,
                message_type,
                completion_id,
            ):
                return True
            manager.create_message(
                from_session=from_session,
                to_session=session_id,
                content=content,
                message_type=message_type,
                priority=priority,
                metadata_json=json.dumps(metadata, default=str, sort_keys=True),
            )
        return True

    try:
        return bool(await run_db(persist_notification))
    except Exception:
        logger.exception("Failed to send ISM to session %s", session_id)
        return False
