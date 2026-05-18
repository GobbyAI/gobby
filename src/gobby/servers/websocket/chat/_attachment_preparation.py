"""Chat attachment preparation wrapper."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from gobby.servers.websocket.chat_attachments import (
    PreparedMessageAttachments,
    prepare_message_attachments,
)

logger = logging.getLogger(__name__)


class SendError(Protocol):
    async def __call__(
        self,
        websocket: Any,
        message: str,
        request_id: str | None = None,
        code: str = "ERROR",
    ) -> None: ...


async def prepare_chat_attachments_or_error(
    owner: Any,
    websocket: Any,
    attachments: Any,
    *,
    conversation_id: str,
    message_id: str | None,
    request_id: str,
    send_error: SendError,
) -> PreparedMessageAttachments | None:
    """Prepare attachments or send the request-scoped chat error."""
    try:
        return await prepare_message_attachments(
            owner,
            attachments,
            conversation_id=conversation_id,
            message_id=message_id,
        )
    except ValueError as exc:
        await send_error(websocket, str(exc), request_id=request_id)
        return None
    except Exception:
        logger.exception(
            "Unexpected chat attachment preparation failure for conversation %s request_id=%s",
            conversation_id,
            request_id,
        )
        await send_error(
            websocket,
            "Failed to prepare attachments. Check daemon logs for details.",
            request_id=request_id,
            code="ATTACHMENT_PREP_FAILED",
        )
        return None
