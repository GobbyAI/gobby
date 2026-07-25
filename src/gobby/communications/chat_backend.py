"""ChatSession responder backend for communications channels."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol, cast

from gobby.communications.chat_transport import (
    CommunicationsChatStreamTransport,
    CommunicationsDeliveryManager,
)
from gobby.communications.models import CommsAttachment, CommsMessage
from gobby.communications.responder import ResponderContext
from gobby.communications.tts_voice import synthesize_telegram_voice
from gobby.servers.chat_stream_transport import ChatStreamTransport
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.voice.tts import TTSProvider

logger = logging.getLogger(__name__)

_TYPING_REFRESH_SECONDS = 4.0
_DEFAULT_AGENT = "comms-agent"
_DEFAULT_CHAT_MODE = "normal"

VoiceSynthesizer = Callable[[TTSProvider, str], Awaitable[bytes]]


class AttachmentStore(Protocol):
    """Attachment storage surface required by generated voice notes."""

    async def store(self, content: bytes, filename: str) -> Path: ...


class CommunicationsVoiceDeliveryManager(CommunicationsDeliveryManager, Protocol):
    """Manager surface required to persist and deliver generated voice notes."""

    @property
    def attachment_manager(self) -> AttachmentStore: ...

    async def send_attachment(
        self,
        channel_name: str,
        file_path: Path,
        filename: str | None = None,
        content_type: str = "application/octet-stream",
        content: str = "",
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[CommsMessage, CommsAttachment]: ...


class ChatTurnHost(Protocol):
    """ChatSession orchestration surface used by the communications backend."""

    async def _run_chat_turn(
        self,
        *,
        conversation_id: str,
        content: str,
        model: str | None,
        transport: ChatStreamTransport,
        project_id: str | None = None,
        provider: str | None = None,
        tts_enabled: bool | None = None,
    ) -> None: ...

    async def reset_chat_session(self, conversation_id: str) -> bool: ...

    async def configure_chat_session(
        self,
        conversation_id: str,
        *,
        chat_mode: str,
        agent_name: str,
        project_id: str,
    ) -> None: ...

    def resolve_chat_binding(
        self,
        conversation_id: str,
        *,
        provider: str | None,
        model: str | None,
    ) -> tuple[str, str | None]: ...


class ChatSessionCommsBackend:
    """Run responder messages through persistent ChatSession instances."""

    def __init__(
        self,
        host: ChatTurnHost,
        manager: CommunicationsVoiceDeliveryManager,
        *,
        tts_provider_getter: Callable[[], TTSProvider | None] | None = None,
        voice_synthesizer: VoiceSynthesizer = synthesize_telegram_voice,
    ) -> None:
        self._host = host
        self._manager = manager
        self._tts_provider_getter = tts_provider_getter
        self._voice_synthesizer = voice_synthesizer
        self._active_turns: dict[str, asyncio.Task[None]] = {}

    async def run_turn(self, context: ResponderContext) -> str | None:
        """Run one inbound message through its comms-session ChatSession."""
        session_key = _session_key(context)
        current = asyncio.current_task()
        if current is not None:
            self._active_turns[session_key] = cast(asyncio.Task[None], current)
        tts_provider = self._get_tts_provider(context)
        transport = CommunicationsChatStreamTransport(
            self._manager,
            context,
            defer_delivery=tts_provider is not None,
        )
        voice_delivered = False
        typing_task: asyncio.Task[None] | None = None
        project_id = _string_setting(context, "project_id") or PERSONAL_PROJECT_ID
        try:
            await self._host.configure_chat_session(
                session_key,
                chat_mode=_string_setting(context, "chat_mode") or _DEFAULT_CHAT_MODE,
                agent_name=_string_setting(context, "agent") or _DEFAULT_AGENT,
                project_id=project_id,
            )
            typing_task = await self._start_typing(context)
            await self._host._run_chat_turn(
                conversation_id=session_key,
                content=context.message.content,
                model=_string_setting(context, "model"),
                project_id=project_id,
                transport=transport,
                provider=_string_setting(context, "provider"),
                tts_enabled=False,
            )
            if (
                tts_provider is not None
                and transport.text.strip()
                and not transport.has_delivered_text
            ):
                voice_delivered = await self._send_voice_reply(
                    context,
                    tts_provider,
                    transport.text,
                )
        finally:
            if typing_task is not None:
                typing_task.cancel()
                await asyncio.gather(typing_task, return_exceptions=True)
            try:
                if tts_provider is not None and not voice_delivered:
                    await transport.release_deferred_text()
                else:
                    await transport.finalize()
            finally:
                if current is not None and self._active_turns.get(session_key) is current:
                    self._active_turns.pop(session_key, None)
        return None

    def _get_tts_provider(self, context: ResponderContext) -> TTSProvider | None:
        if (
            context.channel.channel_type != "telegram"
            or not _boolean_setting(context, "tts_enabled")
            or self._tts_provider_getter is None
        ):
            return None
        try:
            return self._tts_provider_getter()
        except Exception:
            logger.warning("Failed to resolve Telegram TTS provider", exc_info=True)
            return None

    async def _send_voice_reply(
        self,
        context: ResponderContext,
        provider: TTSProvider,
        text: str,
    ) -> bool:
        try:
            voice = await self._voice_synthesizer(provider, text)
            file_path = await self._manager.attachment_manager.store(voice, "reply.ogg")
            message, _attachment = await self._manager.send_attachment(
                context.channel.name,
                file_path,
                filename="reply.ogg",
                content_type="audio/ogg",
                content=text,
                session_id=context.message.session_id,
                metadata={
                    "platform_destination": context.conversation_id,
                    "voice_note": True,
                },
            )
            if message.status != "sent":
                raise RuntimeError(message.error or "Telegram voice-note delivery failed")
        except Exception:
            logger.warning(
                "Failed to send Telegram TTS voice note; falling back to text",
                exc_info=True,
            )
            return False
        return True

    async def _start_typing(self, context: ResponderContext) -> asyncio.Task[None] | None:
        channel_name = context.channel.name
        if not self._manager.supports_typing(channel_name):
            return None
        try:
            await self._manager.send_typing(channel_name, context.conversation_id)
        except Exception:
            logger.warning(
                "Failed to publish initial typing indicator for channel %s",
                channel_name,
                exc_info=True,
            )
            return None
        return asyncio.create_task(
            self._refresh_typing(channel_name, context.conversation_id),
            name=f"comms-typing:{channel_name}:{context.conversation_id}",
        )

    async def _refresh_typing(self, channel_name: str, conversation_id: str) -> None:
        while True:
            await asyncio.sleep(_TYPING_REFRESH_SECONDS)
            try:
                await self._manager.send_typing(channel_name, conversation_id)
            except Exception:
                logger.warning(
                    "Failed to refresh typing indicator for channel %s",
                    channel_name,
                    exc_info=True,
                )
                return

    async def new_session(self, context: ResponderContext) -> str | None:
        """Discard the current runtime so the next message starts fresh."""
        await self._reset_session(context)
        return "Started a new conversation."

    async def reset_session(self, context: ResponderContext) -> str | None:
        """Reset the current runtime conversation."""
        await self._reset_session(context)
        return "Conversation reset."

    async def stop_turn(self, context: ResponderContext) -> str | None:
        """Cancel the active turn for this comms session."""
        session_key = _session_key(context)
        task = self._active_turns.get(session_key)
        if task is None or task.done():
            return "No active turn."
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return "Stopped the active turn."

    async def status(self, context: ResponderContext) -> str | None:
        """Describe the current responder runtime binding."""
        session_key = _session_key(context)
        state = "active" if session_key in self._active_turns else "idle"
        provider, model = self._host.resolve_chat_binding(
            session_key,
            provider=_string_setting(context, "provider"),
            model=_string_setting(context, "model"),
        )
        model = model or "provider default"
        return f"Responder {state}. Provider: {provider}. Model: {model}."

    async def help(self, context: ResponderContext) -> str | None:
        """Return the supported communications command list."""
        return "Commands: /new, /reset, /stop, /status, /help"

    async def _reset_session(self, context: ResponderContext) -> None:
        session_key = _session_key(context)
        task = self._active_turns.get(session_key)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._host.reset_chat_session(session_key)


def _session_key(context: ResponderContext) -> str:
    session_id = context.message.session_id
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Responder message is not linked to a communications session")
    return session_id


def _string_setting(context: ResponderContext, key: str) -> str | None:
    value = context.responder_config.get(key)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _boolean_setting(context: ResponderContext, key: str) -> bool:
    """Return a strict boolean responder setting."""
    return context.responder_config.get(key) is True
