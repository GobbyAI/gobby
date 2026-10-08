"""Outbound communications operations."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from gobby.communications.models import (
    ChannelConfig,
    ChannelNotFoundError,
    CommsAttachment,
    CommsMessage,
)
from gobby.communications.telegram_callbacks import bounded_callback_ttl

if TYPE_CHECKING:
    from gobby.communications.manager import CommunicationsManager

logger = logging.getLogger(__name__)
_OUTBOUND_DRAIN_SECONDS = 2.0
_OUTBOUND_CANCEL_SECONDS = 0.5


class OutboundCommunications:
    """Sends messages, attachments, routed events, and proactive messages."""

    def __init__(self, manager: CommunicationsManager) -> None:
        self._manager = manager
        self._pending: set[asyncio.Task[Any]] = set()
        self._messages: dict[asyncio.Task[Any], CommsMessage] = {}
        self._stopping = False

    def start(self) -> None:
        self._stopping = False

    async def run[T](self, operation: Coroutine[Any, Any, T]) -> T:
        """Own delivery independently of a request or responder's cancellation."""
        if self._stopping:
            operation.close()
            raise RuntimeError("Outbound communications are stopping")
        task = asyncio.create_task(operation)
        self._pending.add(task)
        task.add_done_callback(self._finished)
        return await asyncio.shield(task)

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._pending.discard(task)
        self._messages.pop(task, None)
        if not task.cancelled():
            task.exception()

    async def stop(self, *, drain_seconds: float | None = None) -> None:
        """Drain accepted sends before channel transports close."""
        self._stopping = True
        pending = tuple(self._pending)
        if pending:
            try:
                _, unfinished = await asyncio.wait(
                    pending,
                    timeout=_OUTBOUND_DRAIN_SECONDS if drain_seconds is None else drain_seconds,
                )
            except asyncio.CancelledError:
                await self._cancel_pending(
                    {task for task in pending if not task.done()}, "CancelledError"
                )
                raise
            await self._cancel_pending(unfinished, "TimeoutError")

    async def _cancel_pending(self, pending: set[asyncio.Task[Any]], reason: str) -> None:
        for task in pending:
            message = self._messages.get(task)
            if message is not None:
                logger.error(
                    "Outbound message %s: %s: shutdown settlement expired; last delivery status %s; inspect reservation",
                    message.id,
                    reason,
                    message.status,
                )
            task.cancel()
        if pending:
            # An adapter may suppress cancellation. Its durable reservation survives
            # the daemon's existing hard process-exit boundary.
            await asyncio.wait(pending, timeout=_OUTBOUND_CANCEL_SECONDS)

    async def _reserve(
        self, message: CommsMessage, attachment: CommsAttachment | None = None
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._messages[task] = message
        try:
            await asyncio.to_thread(self._manager._store.create_message, message)
            if attachment is not None:
                await asyncio.to_thread(self._manager._store.create_attachment, attachment)
        except Exception as exc:
            logger.exception(
                "Failed to reserve outbound message %s: %s: %s", message.id, type(exc).__name__, exc
            )
            raise

    async def _record_result(self, message: CommsMessage) -> None:
        try:
            await asyncio.to_thread(
                self._manager._store.update_message_delivery,
                message.id,
                message.status,
                message.error,
                message.platform_message_id,
                message.metadata_json,
            )
        except Exception as exc:
            logger.exception(
                "Failed to store outbound result %s: %s: %s",
                message.id,
                type(exc).__name__,
                exc,
            )

    def _cancelled(self, message: CommsMessage, channel_name: str) -> None:
        message.status = "failed"
        message.error = "CancelledError: outbound shutdown deadline expired; delivery uncertain"
        logger.error("Failed to send message %s to %r: %s", message.id, channel_name, message.error)

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
        if channel.channel_type == "telegram":
            label = await asyncio.to_thread(manager.telegram_sender_label, channel, session_id)
            if label is None:
                effective.pop("telegram_sender_label", None)
            else:
                effective["telegram_sender_label"] = label
        if session_id:
            attached = await asyncio.to_thread(manager.attached_destination, channel.id, session_id)
            if attached is not None:
                kind, _, destination = attached.partition(":")
                chat_id, _, thread_id = destination.partition(":")
                effective.setdefault("platform_destination", chat_id)
                if kind == "topic":
                    effective.setdefault("thread_id", thread_id)
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

    async def _require_session(self, session_id: str | None) -> None:
        """Reject an outbound session id before delivery unless a session has exactly that id.

        Reject invalid caller input before reserving a row or reaching the channel.
        """
        if session_id is not None and not await asyncio.to_thread(
            self._manager._store.session_exists, session_id
        ):
            raise ValueError(f"Unknown session {session_id!r}")

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
            raise ChannelNotFoundError(channel_name)

        channel = manager._channel_by_name[channel_name]
        await self._require_session(session_id)

        platform_thread_id = None
        if session_id:
            platform_thread_id = manager._get_thread_id(channel.id, session_id)
        effective_metadata = await self.enrich_metadata(
            channel,
            channel_name,
            session_id,
            metadata,
        )
        if "callback_ttl_seconds" in effective_metadata:
            # Caller input: reject before a failed row or daemon error is recorded.
            bounded_callback_ttl(effective_metadata["callback_ttl_seconds"])
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

        await self._reserve(message)
        try:
            await manager._rate_limiter.wait_if_needed(channel.id)
            platform_message_id = await adapter.send_message(message)
            message.platform_message_id = platform_message_id
            message.status = "sent"
        except asyncio.CancelledError:
            self._cancelled(message, channel_name)
            raise
        except Exception as e:
            message.status = "failed"
            message.error = str(e) or type(e).__name__
            logger.exception(
                "Failed to send message %s to %r: %s: %s",
                message.id,
                channel_name,
                type(e).__name__,
                e,
            )
        finally:
            await self._record_result(message)

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
            raise ChannelNotFoundError(channel_name)

        channel = manager._channel_by_name[channel_name]
        await self._require_session(session_id)
        size_bytes = file_path.stat().st_size

        if not manager.attachment_manager.validate_size(size_bytes, channel.channel_type):
            limit = manager.attachment_manager.get_size_limit(channel.channel_type)
            raise ValueError(
                f"File size {size_bytes} exceeds {channel.channel_type} limit of {limit} bytes"
            )

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

        await self._reserve(message, attachment)
        try:
            await manager._rate_limiter.wait_if_needed(channel.id)
            platform_message_id = await adapter.send_attachment(message, attachment, file_path)
            message.platform_message_id = platform_message_id
            message.status = "sent"
        except asyncio.CancelledError:
            self._cancelled(message, channel_name)
            raise
        except NotImplementedError:
            message.status = "failed"
            message.error = f"{channel.channel_type} adapter does not support file attachments"
            logger.error(
                "Failed to send attachment %s to %r: NotImplementedError: %s",
                message.id,
                channel_name,
                message.error,
            )
        except Exception as e:
            message.status = "failed"
            message.error = str(e) or type(e).__name__
            logger.exception(
                "Failed to send attachment %s to %r: %s: %s",
                message.id,
                channel_name,
                type(e).__name__,
                e,
            )
        finally:
            await self._record_result(message)

        if manager.event_callback is not None:
            try:
                await manager.event_callback(
                    "comms.attachment_sent", message=message, attachment=attachment
                )
            except Exception as e:
                logger.warning("Event callback error on send_attachment: %s", e, exc_info=True)

        return message, attachment

    async def send_proactive(
        self, channel_name: str, conversation_id: str, content: str, content_type: str = "text"
    ) -> CommsMessage:
        """Send a proactive message via an adapter that supports it."""
        manager = self._manager
        adapter = manager._adapters.get(channel_name)
        if adapter is None:
            raise ChannelNotFoundError(channel_name)

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

        await self._reserve(message)
        try:
            await manager._rate_limiter.wait_if_needed(channel.id)
            message.platform_message_id = await adapter.send_proactive(
                conversation_id, content, content_type
            )
            message.status = "sent"
        except asyncio.CancelledError:
            self._cancelled(message, channel_name)
            raise
        except NotImplementedError as exc:
            message.status = "failed"
            message.error = str(exc) or type(exc).__name__
            logger.error(
                "Failed to send proactive message %s to %r: NotImplementedError: %s",
                message.id,
                channel_name,
                message.error,
            )
            raise ValueError(
                f"Channel {channel_name!r} does not support proactive messaging"
            ) from exc
        except Exception as exc:
            message.status = "failed"
            message.error = str(exc) or type(exc).__name__
            logger.exception(
                "Failed to send proactive message %s to %r: %s: %s",
                message.id,
                channel_name,
                type(exc).__name__,
                exc,
            )
        finally:
            await self._record_result(message)

        if manager.event_callback is not None:
            try:
                await manager.event_callback("comms.message_sent", message=message)
            except Exception as exc:
                logger.warning("Event callback error on send_proactive: %s", exc, exc_info=True)

        return message
