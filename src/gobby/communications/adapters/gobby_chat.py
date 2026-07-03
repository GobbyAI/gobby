"""Gobby Chat channel adapter.

Wraps the existing WebSocket chat infrastructure as a communications
channel adapter so that routing rules can target gobby_chat alongside
external channels like Slack and Telegram.

This adapter is an internal bridge — it doesn't call external APIs.
Messages are broadcast to connected WebSocket clients via the
WebSocketServer's broadcast method.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from gobby.communications.adapters import register_adapter
from gobby.communications.adapters.base import BaseChannelAdapter
from gobby.communications.models import (
    ChannelCapabilities,
    ChannelConfig,
    CommsMessage,
)

logger = logging.getLogger(__name__)


class GobbyChatAdapter(BaseChannelAdapter):
    """Adapter that bridges the communications framework to WebSocket chat.

    Unlike external adapters (Slack, Telegram), this adapter doesn't use
    HTTP clients or external APIs.  Instead it holds a reference to the
    WebSocketServer's broadcast callable and pushes outbound messages as
    WebSocket events to connected web-UI clients.

    The adapter is special-cased:
    - ``supports_webhooks`` and ``supports_polling`` are False — inbound
      messages arrive through the WebSocket chat handler, not webhooks.
    - ``initialize`` only needs a broadcast callback (no secrets).
    - ``parse_webhook`` / ``verify_webhook`` raise NotImplementedError.
    """

    def __init__(self) -> None:
        super().__init__()
        self._broadcast: Callable[..., Any] | None = None
        self._initialized = False

    @property
    def channel_type(self) -> str:
        return "gobby_chat"

    @property
    def max_message_length(self) -> int:
        return 100_000

    @property
    def supports_webhooks(self) -> bool:
        return False

    @property
    def supports_polling(self) -> bool:
        return False

    async def initialize(
        self, config: ChannelConfig, secret_resolver: Callable[[str], str | None]
    ) -> None:
        """Initialize the Gobby Chat adapter.

        The adapter reads an optional ``broadcast_ref`` from the channel
        config.  When wired through CommunicationsManager, the broadcast
        callable is injected directly via :meth:`set_broadcast`.

        No external credentials are required.
        """
        self._initialized = True
        logger.info("GobbyChatAdapter initialized")

    def set_broadcast(self, broadcast: Callable[..., Any]) -> None:
        """Inject the WebSocket broadcast callable after initialization.

        Args:
            broadcast: An async callable that broadcasts a dict to
                connected WebSocket clients (typically
                ``WebSocketServer.broadcast``).
        """
        self._broadcast = broadcast

    async def send_message(self, message: CommsMessage) -> str | None:
        """Broadcast message to connected WebSocket clients.

        Sends a ``comms_message`` event via WebSocket so that web-UI
        clients can render cross-channel messages (e.g., a Telegram
        message routed to web chat).

        Returns the message ID as the platform message ID after the
        WebSocket broadcast succeeds.
        """
        if not self._initialized:
            raise RuntimeError("GobbyChatAdapter not initialized")

        if self._broadcast is None:
            raise RuntimeError("GobbyChatAdapter broadcast callable is not configured")

        payload = {
            "type": "comms_message",
            "channel_type": "gobby_chat",
            "message_id": message.id,
            "content": message.content,
            "content_type": message.content_type,
            "session_id": message.session_id,
            "direction": message.direction,
            "metadata": message.metadata_json,
        }

        try:
            await self._broadcast(payload)
        except Exception as e:
            logger.error(f"Failed to broadcast Gobby Chat message: {e}", exc_info=True)
            raise

        return message.id

    async def shutdown(self) -> None:
        """No-op — the WebSocket server has its own lifecycle."""
        self._broadcast = None
        self._initialized = False

    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            threading=True,
            reactions=False,
            files=True,
            markdown=True,
            max_message_length=self.max_message_length,
        )

    def parse_webhook(
        self, payload: dict[str, Any] | bytes, headers: dict[str, str]
    ) -> list[CommsMessage]:
        """Not applicable — inbound messages arrive via WebSocket chat handler."""
        raise NotImplementedError(
            "GobbyChatAdapter does not support webhooks; "
            "inbound messages arrive via the WebSocket chat handler"
        )

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        """Not applicable — no inbound webhook support."""
        raise NotImplementedError("GobbyChatAdapter does not support webhook verification")

    def parse_inbound(self, data: dict[str, Any]) -> CommsMessage | None:
        """Parse an inbound WebSocket chat message into a CommsMessage.

        This is a convenience method for the WebSocket chat handler to
        convert raw chat messages into CommsMessage objects that can be
        fed into the communications framework for routing/storage.

        Args:
            data: Raw WebSocket message dict with at minimum ``content``
                and ``conversation_id`` fields.

        Returns:
            A CommsMessage or None if the data is insufficient.
        """
        content = data.get("content")
        if not content or not isinstance(content, str):
            return None
        user_id = data.get("user_id")
        if not user_id or not isinstance(user_id, str):
            return None

        from datetime import UTC, datetime
        from uuid import uuid4

        metadata = {
            k: v
            for k, v in data.items()
            if k
            not in (
                "content",
                "message_id",
                "conversation_id",
                "user_id",
                "username",
                "type",
            )
        }
        metadata.setdefault("platform_channel_id", "gobby_chat")
        metadata["external_username"] = data.get("username") or user_id

        return CommsMessage(
            id=data.get("message_id") or str(uuid4()),
            channel_id="",
            direction="inbound",
            content=content,
            created_at=datetime.now(UTC),
            session_id=data.get("conversation_id"),
            identity_id=user_id,
            content_type="text",
            metadata_json=metadata,
        )


# Register the adapter
register_adapter("gobby_chat", GobbyChatAdapter)
