"""Authenticate the operator's attached communications wake intent."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gobby.communications.manager import CommunicationsManager
    from gobby.communications.models import ChannelConfig, CommsMessage


async def is_attached_operator_message(
    manager: CommunicationsManager,
    channel: ChannelConfig,
    message: CommsMessage,
    session_id: str,
) -> bool:
    """Authorize replacement only for the enrolled operator's attached direct conversation."""
    if message.metadata_json.get("conversation_type") != "private":
        return False
    try:
        operator = await asyncio.to_thread(manager.resolve_operator_conversation, channel.name)
    except ValueError:
        return False
    conversation_id = message.metadata_json.get("conversation_id")
    if not conversation_id:
        conversation_id = f"dm:{message.metadata_json.get('chat_id')}"
    return (
        operator.get("session_id") == session_id
        and operator.get("identity_id") == message.identity_id
        and operator.get("conversation_id") == conversation_id
    )
