"""ChatSession responder backend for communications channels."""

from __future__ import annotations

import asyncio
from typing import Protocol, cast

from gobby.communications.chat_transport import (
    CommunicationsChatStreamTransport,
    CommunicationsDeliveryManager,
)
from gobby.communications.responder import ResponderContext
from gobby.servers.chat_stream_transport import ChatStreamTransport


class ChatTurnHost(Protocol):
    """ChatSession orchestration surface used by the communications backend."""

    async def _run_chat_turn(
        self,
        *,
        conversation_id: str,
        content: str,
        model: str | None,
        transport: ChatStreamTransport,
        provider: str | None = None,
        tts_enabled: bool | None = None,
    ) -> None: ...

    async def reset_chat_session(self, conversation_id: str) -> bool: ...


class ChatSessionCommsBackend:
    """Run responder messages through persistent ChatSession instances."""

    def __init__(
        self,
        host: ChatTurnHost,
        manager: CommunicationsDeliveryManager,
    ) -> None:
        self._host = host
        self._manager = manager
        self._active_turns: dict[str, asyncio.Task[None]] = {}

    async def run_turn(self, context: ResponderContext) -> str | None:
        """Run one inbound message through its comms-session ChatSession."""
        session_key = _session_key(context)
        current = asyncio.current_task()
        if current is not None:
            self._active_turns[session_key] = cast(asyncio.Task[None], current)
        transport = CommunicationsChatStreamTransport(self._manager, context)
        try:
            await self._host._run_chat_turn(
                conversation_id=session_key,
                content=context.message.content,
                model=_string_setting(context, "model"),
                transport=transport,
                provider=_string_setting(context, "provider"),
                tts_enabled=False,
            )
        finally:
            try:
                await transport.finalize()
            finally:
                if current is not None and self._active_turns.get(session_key) is current:
                    self._active_turns.pop(session_key, None)
        return None

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
        provider = _string_setting(context, "provider") or "default"
        model = _string_setting(context, "model") or "default"
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
