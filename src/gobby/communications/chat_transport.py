"""Communications delivery transport for streamed ChatSession turns."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, Protocol

from gobby.communications.adapters.base import MessageEditNotApplied
from gobby.communications.models import CommsMessage
from gobby.communications.responder import ResponderContext

_THINKING_PLACEHOLDER = "Thinking…"
_INITIAL_STREAM_TEXT_LENGTH = 24


class CommunicationsDeliveryManager(Protocol):
    """Manager surface required by the communications stream transport."""

    def supports_message_edit(self, channel_name: str) -> bool: ...

    def attached_destination(self, channel_id: str, session_id: str) -> str | None: ...

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
        self._completed_only = context.channel.channel_type == "telegram"
        self._session_id = context.message.session_id
        self._completed = False
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

    @property
    def completed(self) -> bool:
        """Whether the provider completed this response successfully."""
        return self._completed

    @property
    def session_id(self) -> str | None:
        """Return the runtime identity, which may differ from the inbound routing key."""
        return self._session_id

    async def uses_transcript_mirror(self) -> bool:
        """Let the durable transcript sink own replies to this attached conversation."""
        if not self._completed_only or not self.session_id:
            return False
        destination = await asyncio.to_thread(
            self._manager.attached_destination, self._context.channel.id, self.session_id
        )
        kind = "group" if self._context.is_group else "dm"
        conversation = f"{kind}:{self._context.conversation_id}"
        thread = self._context.message.platform_thread_id
        if thread:
            conversation = f"topic:{self._context.conversation_id}:{thread}"
        return destination == conversation

    def base_msg(self, **fields: Any) -> dict[str, Any]:
        """Build a transport-neutral stream frame."""
        return dict(fields)

    async def send_direct(self, msg: dict[str, Any]) -> None:
        """Handle request-scoped terminal frames."""
        if msg.get("type") == "chat_error":
            error = msg.get("error")
            if not self._finalized and isinstance(error, str) and error.strip():
                self._text = error
                self._completed = True
                # Errors have no completed transcript reply to mirror.
                await self._send(error)
                self._last_delivered_text = error
                self._finalized = True
            return
        await self.safe_send(msg)

    async def safe_send(self, msg: dict[str, Any]) -> bool:
        """Collect text chunks and deliver throttled or final updates."""
        if msg.get("type") == "session_info":
            session_id = msg.get("db_session_id")
            if isinstance(session_id, str) and session_id:
                self._session_id = session_id
            return True
        if self._finalized:
            return True
        if msg.get("type") == "tool_status" and not self._finalized:
            # Narration before a tool call is not the answer delivered to the channel.
            self._text = ""
            return True
        if msg.get("type") != "chat_stream":
            return True

        if msg.get("interrupted") is True:
            self._text = ""
            self._finalized = True
            return True

        content = msg.get("content")
        if isinstance(content, str):
            self._text += content

        if msg.get("done") is True:
            self._completed = True

        if self._defer_delivery:
            return True
        if msg.get("done") is True:
            await self.finalize()
        elif content and self._supports_edit and not self._completed_only:
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
        self._last_edit_at = now
        try:
            await self._edit(self._text)
        except MessageEditNotApplied:
            # A later update or the final delivery carries this text (#23292).
            return
        self._last_delivered_text = self._text

    async def _finalize(self) -> None:
        if self._completed_only and not self._completed:
            return
        if self._finalized or not self._text.strip():
            return
        self._finalized = True

        if await self.uses_transcript_mirror():
            return

        if self._last_delivered_text == self._text:
            return
        if not self._supports_edit or not self._initial_sent:
            await self._send(self._text)
            self._last_delivered_text = self._text
            return
        if self._platform_message_id is None:
            await self._send(self._text)
        else:
            try:
                await self._edit(self._text)
            except MessageEditNotApplied:
                # The platform refused the edit; deliver the whole answer anew (#23292).
                await self._send(self._text)
        self._last_delivered_text = self._text

    async def _send(self, content: str) -> CommsMessage:
        return await self._manager.send_message(
            self._context.channel.name,
            content,
            session_id=self.session_id,
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
