"""Inbound communications operations."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from gobby.communications.models import CommsAttachment, CommsMessage
from gobby.communications.webhook_verification import verify_webhook_with_timeout

if TYPE_CHECKING:
    from gobby.communications.manager import CommunicationsManager

logger = logging.getLogger(__name__)
_WEBHOOK_VERIFICATION_TIMEOUT_SECONDS = 5.0


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
        adapter = manager._adapters.get(channel_name)

        handled: list[CommsMessage] = []
        stored: list[CommsMessage] = []
        for message in messages:
            downloaded_attachments: list[CommsAttachment] = []
            try:
                platform_channel_id = (
                    message.metadata_json.get("platform_channel_id")
                    or message.channel_id
                    or message.metadata_json.get("chat_id")
                )
                if platform_channel_id:
                    message.metadata_json["platform_channel_id"] = str(platform_channel_id)
                message.channel_id = channel.id

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
                            logger.exception("Failed to handle reaction: %s", e)
                    continue

                if message.platform_message_id:
                    existing = await asyncio.to_thread(
                        manager._store.get_message_by_platform_id,
                        channel_name,
                        message.platform_message_id,
                    )
                    if existing is not None:
                        handled.append(message)
                        continue

                if message.identity_id:
                    external_user_id = message.identity_id
                    message.metadata_json["external_user_id"] = external_user_id
                    external_username = message.metadata_json.get("external_username")
                    identity_meta: dict[str, Any] = {}
                    if "conversation_reference" in message.metadata_json:
                        identity_meta["conversation_reference"] = message.metadata_json[
                            "conversation_reference"
                        ]

                    identity = await asyncio.to_thread(
                        manager._identity_manager.resolve_identity,
                        channel.id,
                        external_user_id,
                        external_username,
                        metadata=identity_meta,
                    )
                    message.session_id = identity.session_id
                    message.identity_id = identity.id

                if message.session_id and message.platform_thread_id:
                    manager._track_thread(
                        channel.id, message.session_id, message.platform_thread_id
                    )

                if adapter is not None:
                    downloaded_attachments = await adapter.download_inbound_attachments(
                        message,
                        manager.attachment_manager,
                    )
                if downloaded_attachments:
                    persisted, saved_attachments = await asyncio.to_thread(
                        manager._store.create_message_with_attachments,
                        message,
                        downloaded_attachments,
                    )
                    saved_attachment_ids = {attachment.id for attachment in saved_attachments}
                    orphan_paths = [
                        attachment.local_path
                        for attachment in downloaded_attachments
                        if attachment.id not in saved_attachment_ids and attachment.local_path
                    ]
                    if orphan_paths:
                        await asyncio.to_thread(
                            manager.attachment_manager.delete_paths,
                            orphan_paths,
                        )
                else:
                    persisted = await asyncio.to_thread(manager._store.create_message, message)
                downloaded_attachments = []
                stored.append(persisted)
                handled.append(persisted)
            except Exception as e:
                local_paths = [
                    attachment.local_path
                    for attachment in downloaded_attachments
                    if attachment.local_path
                ]
                if local_paths:
                    await asyncio.to_thread(
                        manager.attachment_manager.delete_paths,
                        local_paths,
                    )
                logger.exception("Failed to process inbound message: %s", e)

        if manager.event_callback is not None:
            for msg in stored:
                try:
                    await manager.event_callback("comms.message_received", message=msg)
                except Exception as e:
                    logger.warning(
                        "Event callback error on handle_inbound_messages: %s",
                        e,
                        exc_info=True,
                    )

        return handled

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

        webhook_secret = channel.webhook_secret
        if webhook_secret and webhook_secret.startswith("$secret:"):
            resolved = await asyncio.to_thread(
                manager._secret_store.get,
                webhook_secret.removeprefix("$secret:"),
            )
            if resolved is None:
                raise ValueError(f"Webhook secret for channel {channel_name!r} is not configured")
            webhook_secret = resolved

        verify_bytes: bytes
        if raw_body is not None:
            verify_bytes = raw_body
        elif isinstance(payload, bytes):
            verify_bytes = payload
        else:
            raise ValueError("raw_body must be provided for webhook signature verification")

        try:
            verified = await verify_webhook_with_timeout(
                adapter,
                verify_bytes,
                headers,
                webhook_secret or "",
                _WEBHOOK_VERIFICATION_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise ValueError(
                f"Webhook signature verification failed for channel {channel_name!r}"
            ) from exc
        if not verified:
            raise ValueError(f"Webhook signature verification failed for channel {channel_name!r}")

        parsed: list[CommsMessage] = adapter.parse_webhook(payload, headers)

        for msg in parsed:
            if msg.content_type in {"url_verification", "interaction_ping"}:
                if not msg.channel_id:
                    msg.channel_id = channel.id
                return parsed

        return await self.handle_messages(channel_name, parsed)
