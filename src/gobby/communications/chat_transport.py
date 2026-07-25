"""Communications delivery transport for streamed ChatSession turns."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol

from gobby.communications.models import CommsMessage
from gobby.communications.responder import ResponderContext

_THINKING_PLACEHOLDER = "Thinking…"
_INITIAL_STREAM_TEXT_LENGTH = 24


class CommunicationsDeliveryManager(Protocol):
    """Manager surface required by the communications stream transport."""

    def supports_message_edit(self, channel_name: str) -> bool: ...

    def supports_typing(self, channel_name: str) -> bool: ...

    async def send_typing(self, channel_name: str, conversation_id: str) -> None: ...

    async def send_message(
        self,
        channel_name: str,
        content: str,
        session_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> CommsMessage: ...

    async def edit_message(
        self,
        channel_name: str,
        platform_message_id: str,
        content: str,
        conversation_id: str,
    ) -> None: ...


class CommunicationsChatStreamTransport:
    """Collect streamed text and deliver it through a communications channel."""

    def __init__(
        self,
        manager: CommunicationsDeliveryManager,
        context: ResponderContext,
        *,
        edit_interval: float = 1.5,
        clock: Callable[[], float] = time.monotonic,
        defer_delivery: bool = False,
    ) -> None:
        self._manager = manager
        self._context = context
        self._edit_interval = edit_interval
        self._clock = clock
        self._defer_delivery = defer_delivery
        self._supports_edit = manager.supports_message_edit(context.channel.name)
        self._text = ""
        self._last_delivered_text = ""
        self._last_edit_at = 0.0
        self._platform_message_id: str | None = None
        self._initial_sent = False
        self._finalized = False

    @property
    def text(self) -> str:
        """Return all streamed text collected for the current turn."""
        return self._text

    @property
    def has_delivered_text(self) -> bool:
        """Whether any buffered response text has already been delivered."""
        return bool(self._last_delivered_text)

    def base_msg(self, **fields: Any) -> dict[str, Any]:
        """Build a transport-neutral stream frame."""
        return dict(fields)

    async def send_direct(self, msg: dict[str, Any]) -> None:
        """Handle request-scoped terminal frames."""
        if msg.get("type") == "chat_error":
            error = msg.get("error")
            if isinstance(error, str) and error.strip():
                if self._text and not self._text.endswith("\n"):
                    self._text += "\n\n"
                self._text += error
                await self.release_deferred_text()
            return
        await self.safe_send(msg)

    async def safe_send(self, msg: dict[str, Any]) -> bool:
        """Collect text chunks and deliver throttled or final updates."""
        if msg.get("type") != "chat_stream":
            return True

        content = msg.get("content")
        if isinstance(content, str):
            self._text += content

        if self._defer_delivery:
            return True
        if msg.get("done") is True:
            await self.finalize()
        elif content and self._supports_edit:
            await self._maybe_stream_update()
        return True

    async def finalize(self) -> None:
        """Flush the latest collected text exactly once."""
        if self._defer_delivery:
            return
        await self._finalize()

    async def release_deferred_text(self) -> None:
        """Enable delivery and flush text buffered for an alternate response."""
        self._defer_delivery = False
        await self._finalize()

    async def _maybe_stream_update(self) -> None:
        if not self._text:
            return
        if not self._initial_sent:
            initial_content = (
                self._text
                if len(self._text.strip()) >= _INITIAL_STREAM_TEXT_LENGTH
                else _THINKING_PLACEHOLDER
            )
            sent = await self._send(initial_content)
            self._initial_sent = True
            self._platform_message_id = sent.platform_message_id
            self._last_delivered_text = initial_content
            self._last_edit_at = self._clock()
            return
        if self._platform_message_id is None:
            return

        now = self._clock()
        if now - self._last_edit_at < self._edit_interval:
            return
        await self._edit(self._text)
        self._last_delivered_text = self._text
        self._last_edit_at = now

    async def _finalize(self) -> None:
        if self._finalized or not self._text.strip():
            return
        self._finalized = True

        if not self._supports_edit or not self._initial_sent:
            await self._send(self._text)
            self._last_delivered_text = self._text
            return
        if self._last_delivered_text == self._text:
            return
        if self._platform_message_id is not None:
            await self._edit(self._text)
        else:
            await self._send(self._text)
        self._last_delivered_text = self._text

    async def _send(self, content: str) -> CommsMessage:
        return await self._manager.send_message(
            self._context.channel.name,
            content,
            session_id=self._context.message.session_id,
            metadata={"platform_destination": self._context.conversation_id},
        )

    async def _edit(self, content: str) -> None:
        platform_message_id = self._platform_message_id
        if platform_message_id is None:
            return
        await self._manager.edit_message(
            self._context.channel.name,
            platform_message_id,
            content,
            self._context.conversation_id,
        )
