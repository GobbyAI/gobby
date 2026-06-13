"""Inbound communications operations."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from gobby.communications.models import CommsMessage

if TYPE_CHECKING:
    from gobby.communications.manager import CommunicationsManager

logger = logging.getLogger(__name__)


class InboundCommunications:
    """Handles parsed inbound messages and raw webhook payloads."""

    def __init__(self, manager: CommunicationsManager) -> None:
        self._manager = manager

    async def handle_messages(
        self, channel_name: str, messages: list[CommsMessage]
    ) -> list[CommsMessage]:
        """Process, resolve identity, and store a list of inbound messages."""
        manager = self._manager
        channel = manager._channel_by_name.get(channel_name)
        if channel is None:
            raise ValueError(f"Channel {channel_name!r} not found or not active")

        stored: list[CommsMessage] = []
        for message in messages:
            if message.content_type == "reaction":
                if manager.reaction_handler:
                    try:
                        await manager.reaction_handler.handle_reaction(
                            channel_name,
                            message.platform_message_id,
                            message.content,
                            message.identity_id,
                        )
                    except Exception as e:
                        logger.error(f"Failed to handle reaction: {e}", exc_info=True)
                continue

            if not message.channel_id:
                message.channel_id = channel.id

            if message.identity_id:
                external_username = message.metadata_json.get("external_username")
                identity_meta: dict[str, Any] = {}
                if "conversation_reference" in message.metadata_json:
                    identity_meta["conversation_reference"] = message.metadata_json[
                        "conversation_reference"
                    ]

                identity = manager._identity_manager.resolve_identity(
                    channel.id, message.identity_id, external_username, metadata=identity_meta
                )
                message.session_id = identity.session_id
                message.identity_id = identity.id

            if message.session_id and message.platform_thread_id:
                manager._track_thread(channel_name, message.session_id, message.platform_thread_id)

            try:
                manager._store.create_message(message)
                stored.append(message)
            except Exception as e:
                logger.error(f"Failed to store inbound message: {e}")

        if manager.event_callback is not None:
            for msg in stored:
                try:
                    await manager.event_callback("comms.message_received", message=msg)
                except Exception as e:
                    logger.debug(f"Event callback error on handle_inbound_messages: {e}")

        return stored

    async def handle_webhook(
        self,
        channel_name: str,
        payload: dict[str, Any] | bytes,
        headers: dict[str, str],
        raw_body: bytes | None = None,
    ) -> list[CommsMessage]:
        """Handle an inbound webhook payload."""
        manager = self._manager
        adapter = manager._adapters.get(channel_name)
        if adapter is None:
            raise ValueError(f"Channel {channel_name!r} not found or not active")

        channel = manager._channel_by_name[channel_name]

        if channel.webhook_secret:
            verify_bytes: bytes
            if raw_body is not None:
                verify_bytes = raw_body
            elif isinstance(payload, bytes):
                verify_bytes = payload
            else:
                raise ValueError("raw_body must be provided for webhook signature verification")

            if not adapter.verify_webhook(verify_bytes, headers, channel.webhook_secret):
                raise ValueError(
                    f"Webhook signature verification failed for channel {channel_name!r}"
                )

        parsed: list[CommsMessage] = adapter.parse_webhook(payload, headers)

        for msg in parsed:
            if msg.content_type == "url_verification":
                if not msg.channel_id:
                    msg.channel_id = channel.id
                return parsed

        return await self.handle_messages(channel_name, parsed)
