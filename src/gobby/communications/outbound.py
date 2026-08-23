"""Outbound communications operations."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.communications.models import ChannelConfig, CommsAttachment, CommsMessage

if TYPE_CHECKING:
    from gobby.communications.manager import CommunicationsManager

logger = logging.getLogger(__name__)


class OutboundCommunications:
    """Sends messages, attachments, routed events, and proactive messages."""

    def __init__(self, manager: CommunicationsManager) -> None:
        self._manager = manager

    async def enrich_metadata(
        self,
        channel: ChannelConfig,
        channel_name: str,
        session_id: str | None,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build effective metadata for outbound messages/attachments."""
        manager = self._manager
        effective = dict(metadata) if metadata else {}
        if "platform_destination" not in effective:
            default_dest = channel.config_json.get("default_destination")
            if default_dest:
                effective["platform_destination"] = default_dest

        if session_id:
            identity = await asyncio.to_thread(
                manager._identity_manager.get_identity_by_session,
                channel.id,
                session_id,
            )
            if identity and "conversation_reference" in identity.metadata_json:
                conv_ref = identity.metadata_json["conversation_reference"]
                if isinstance(conv_ref, dict):
                    effective.setdefault("conversation_reference", conv_ref)
                    conversation_id = conv_ref.get("conversation_id")
                    if conversation_id and not effective.get("platform_destination"):
                        effective["platform_destination"] = conversation_id
                    service_url = conv_ref.get("service_url")
                    if service_url and not effective.get("service_url"):
                        effective["service_url"] = service_url
                    logger.debug(
                        "Injected conversation_reference for proactive messaging on %s",
                        channel_name,
                    )
        return effective

    async def send_message(
        self,
        channel_name: str,
        content: str,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CommsMessage:
        """Send a message to a named channel."""
        manager = self._manager
        adapter = manager._adapters.get(channel_name)
        if adapter is None:
            raise ValueError(f"Channel {channel_name!r} not found or not active")

        channel = manager._channel_by_name[channel_name]

        platform_thread_id = None
        if session_id:
            platform_thread_id = manager._get_thread_id(channel.id, session_id)
        effective_metadata = await self.enrich_metadata(
            channel,
            channel_name,
            session_id,
            metadata,
        )
        explicit_thread_id = effective_metadata.get("thread_id")
        if isinstance(explicit_thread_id, str) and explicit_thread_id.strip():
            platform_thread_id = explicit_thread_id.strip()

        message = CommsMessage(
            id=str(uuid.uuid4()),
            channel_id=channel.id,
            direction="outbound",
            content=content,
            session_id=session_id,
            status="pending",
            platform_thread_id=platform_thread_id,
            metadata_json=effective_metadata,
            created_at=datetime.now(UTC),
        )

        try:
            await manager._rate_limiter.wait_if_needed(channel.id)
            platform_message_id = await adapter.send_message(message)
            message.platform_message_id = platform_message_id
            message.status = "sent"
        except Exception as e:
            message.status = "failed"
            message.error = str(e)
            logger.exception("Failed to send message to %r: %s", channel_name, e)

        try:
            await asyncio.to_thread(manager._store.create_message, message)
        except Exception as e:
            logger.exception("Failed to store outbound message: %s", e)

        if manager.event_callback is not None:
            try:
                await manager.event_callback("comms.message_sent", message=message)
            except Exception as e:
                logger.warning("Event callback error on send_message: %s", e, exc_info=True)

        return message

    async def send_attachment(
        self,
        channel_name: str,
        file_path: Path,
        filename: str | None = None,
        content_type: str = "application/octet-stream",
        content: str = "",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[CommsMessage, CommsAttachment]:
        """Send a file attachment to a named channel."""
        manager = self._manager
        file_path = Path(file_path)
        if not file_path.exists():
            raise ValueError(f"Attachment file not found: {file_path}")

        adapter = manager._adapters.get(channel_name)
        if adapter is None:
            raise ValueError(f"Channel {channel_name!r} not found or not active")

        channel = manager._channel_by_name[channel_name]
        size_bytes = file_path.stat().st_size

        if not manager.attachment_manager.validate_size(size_bytes, channel.channel_type):
            limit = manager.attachment_manager.get_size_limit(channel.channel_type)
            raise ValueError(
                f"File size {size_bytes} exceeds {channel.channel_type} limit of {limit} bytes"
            )

        await manager._rate_limiter.wait_if_needed(channel.id)

        platform_thread_id = None
        if session_id:
            platform_thread_id = manager._get_thread_id(channel.id, session_id)

        message = CommsMessage(
            id=str(uuid.uuid4()),
            channel_id=channel.id,
            direction="outbound",
            content=content,
            content_type="attachment",
            session_id=session_id,
            status="pending",
            platform_thread_id=platform_thread_id,
            metadata_json=await self.enrich_metadata(channel, channel_name, session_id, metadata),
            created_at=datetime.now(UTC),
        )

        attachment = CommsAttachment(
            id=str(uuid.uuid4()),
            message_id=message.id,
            filename=filename or file_path.name,
            content_type=content_type,
            size_bytes=size_bytes,
            local_path=None,
            created_at=datetime.now(UTC),
        )

        try:
            platform_message_id = await adapter.send_attachment(message, attachment, file_path)
            message.platform_message_id = platform_message_id
            message.status = "sent"
        except NotImplementedError:
            message.status = "failed"
            message.error = f"{channel.channel_type} adapter does not support file attachments"
            logger.error("Adapter %r does not support attachments", channel_name)
        except Exception as e:
            message.status = "failed"
            message.error = str(e)
            logger.exception("Failed to send attachment to %r: %s", channel_name, e)

        try:
            await asyncio.to_thread(manager._store.create_message, message)
            await asyncio.to_thread(manager._store.create_attachment, attachment)
        except Exception as e:
            logger.exception("Failed to store outbound attachment: %s", e)

        if manager.event_callback is not None:
            try:
                await manager.event_callback(
                    "comms.attachment_sent", message=message, attachment=attachment
                )
            except Exception as e:
                logger.warning("Event callback error on send_attachment: %s", e, exc_info=True)

        return message, attachment

    async def send_event(
        self,
        event_type: str,
        content: str,
        project_id: str | None = None,
        session_id: str | None = None,
        *,
        event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> list[CommsMessage]:
        """Route event to matching channels and send to each."""
        manager = self._manager
        channel_ids = await manager._router.match_channels(
            event_type, project_id=project_id, session_id=session_id
        )

        id_to_name: dict[str, str] = {
            channel.id: name for name, channel in manager._channel_by_name.items()
        }

        messages: list[CommsMessage] = []
        for channel_id in channel_ids:
            channel_name = id_to_name.get(channel_id)
            if channel_name is None:
                continue
            try:
                if event_id is None:
                    msg = await manager.send_message(
                        channel_name,
                        content,
                        session_id=session_id,
                        metadata=metadata,
                    )
                else:
                    msg = await self._send_event_once(
                        channel_name,
                        content,
                        event_type=event_type,
                        event_id=event_id,
                        session_id=session_id,
                        metadata=metadata,
                    )
                messages.append(msg)
            except Exception as e:
                logger.error("send_event: failed to send to %r: %s", channel_name, e)

        return messages

    async def _send_event_once(
        self,
        channel_name: str,
        content: str,
        *,
        event_type: str,
        event_id: str,
        session_id: str | None,
        metadata: dict[str, Any] | None,
    ) -> CommsMessage:
        """Reserve and deliver one event message per channel."""
        manager = self._manager
        adapter = manager._adapters.get(channel_name)
        if adapter is None:
            raise ValueError(f"Channel {channel_name!r} not found or not active")

        channel = manager._channel_by_name[channel_name]
        message_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"gobby:communications:event:{event_type}:{event_id}:{channel.id}",
            )
        )
        existing = await asyncio.to_thread(manager._store.get_message, message_id)
        if existing is not None:
            return existing

        platform_thread_id = None
        if session_id:
            platform_thread_id = manager._get_thread_id(channel.id, session_id)
        effective_metadata = await self.enrich_metadata(
            channel,
            channel_name,
            session_id,
            {**(metadata or {}), "source_event_id": event_id},
        )
        message = CommsMessage(
            id=message_id,
            channel_id=channel.id,
            direction="outbound",
            content=content,
            session_id=session_id,
            status="pending",
            platform_thread_id=platform_thread_id,
            metadata_json=effective_metadata,
            created_at=datetime.now(UTC),
        )

        try:
            await asyncio.to_thread(manager._store.create_message, message)
        except Exception:
            existing = await asyncio.to_thread(manager._store.get_message, message_id)
            if existing is not None:
                return existing
            raise

        try:
            await manager._rate_limiter.wait_if_needed(channel.id)
            message.platform_message_id = await adapter.send_message(message)
            message.status = "sent"
        except Exception as exc:
            message.status = "failed"
            message.error = str(exc)
            logger.exception("Failed to send event message to %r: %s", channel_name, exc)

        try:
            await asyncio.to_thread(
                manager._store.update_message_delivery,
                message.id,
                message.status,
                message.error,
                message.platform_message_id,
                message.metadata_json,
            )
        except Exception as exc:
            logger.exception("Failed to update outbound event message: %s", exc)

        if manager.event_callback is not None:
            try:
                await manager.event_callback("comms.message_sent", message=message)
            except Exception as exc:
                logger.warning("Event callback error on send_event: %s", exc, exc_info=True)

        return message

    async def send_proactive(
        self, channel_name: str, conversation_id: str, content: str, content_type: str = "text"
    ) -> CommsMessage:
        """Send a proactive message via an adapter that supports it."""
        manager = self._manager
        adapter = manager._adapters.get(channel_name)
        if adapter is None:
            raise ValueError(f"Channel {channel_name!r} not found or not active")

        channel = manager._channel_by_name[channel_name]
        message = CommsMessage(
            id=str(uuid.uuid4()),
            channel_id=channel.id,
            direction="outbound",
            content=content,
            content_type=content_type,
            status="pending",
            metadata_json={"platform_destination": conversation_id},
            created_at=datetime.now(UTC),
        )

        try:
            await manager._rate_limiter.wait_if_needed(channel.id)
            message.platform_message_id = await adapter.send_proactive(
                conversation_id, content, content_type
            )
            message.status = "sent"
        except NotImplementedError as exc:
            raise ValueError(
                f"Channel {channel_name!r} does not support proactive messaging"
            ) from exc
        except Exception as exc:
            message.status = "failed"
            message.error = str(exc)
            logger.exception("Failed to send proactive message to %r: %s", channel_name, exc)

        try:
            await asyncio.to_thread(manager._store.create_message, message)
        except Exception as exc:
            logger.exception("Failed to store proactive outbound message: %s", exc)

        if manager.event_callback is not None:
            try:
                await manager.event_callback("comms.message_sent", message=message)
            except Exception as exc:
                logger.warning("Event callback error on send_proactive: %s", exc, exc_info=True)

        return message
