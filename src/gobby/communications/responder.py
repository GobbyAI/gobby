"""Inbound communications responder pipeline."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from gobby.communications.group_policy import evaluate_group_message
from gobby.communications.models import ChannelConfig, CommsMessage

logger = logging.getLogger(__name__)

_COMMANDS = frozenset({"start", "new", "reset", "stop", "status", "help"})


class CommunicationsManagerProtocol(Protocol):
    """Manager surface used by the responder."""

    def get_channel(self, channel_id: str) -> ChannelConfig | None: ...

    async def send_message(
        self,
        channel_name: str,
        content: str,
        session_id: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> CommsMessage: ...

    async def set_reaction(
        self,
        channel_name: str,
        conversation_id: str,
        platform_message_id: str,
        reaction: str | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ResponderContext:
    """Normalized context passed to responder backends."""

    channel: ChannelConfig
    message: CommsMessage
    conversation_id: str
    sender_id: str
    is_group: bool
    responder_config: Mapping[str, object]


class ResponderBackend(Protocol):
    """Agent-turn and command operations supplied by the ChatSession transport."""

    async def run_turn(self, context: ResponderContext) -> str | None: ...

    async def new_session(self, context: ResponderContext) -> str | None: ...

    async def reset_session(self, context: ResponderContext) -> str | None: ...

    async def stop_turn(self, context: ResponderContext) -> str | None: ...

    async def status(self, context: ResponderContext) -> str | None: ...

    async def help(self, context: ResponderContext) -> str | None: ...


class ConversationTurnQueue:
    """Serialize turn callbacks per conversation while allowing cross-chat concurrency."""

    def __init__(self) -> None:
        self._tails: dict[str, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def enqueue(
        self,
        conversation_key: str,
        callback: Callable[[], Awaitable[None]],
    ) -> asyncio.Task[None]:
        previous = self._tails.get(conversation_key)

        async def run_after_previous() -> None:
            if previous is not None:
                try:
                    await previous
                except asyncio.CancelledError:
                    if not previous.cancelled():
                        raise
                except Exception:
                    logger.debug(
                        "Continuing queued responder work after a failed prior turn",
                        exc_info=True,
                    )
            await callback()

        task = asyncio.create_task(
            run_after_previous(),
            name=f"comms-responder:{conversation_key}",
        )
        self._tails[conversation_key] = task
        self._tasks.add(task)

        def finish(completed: asyncio.Task[None]) -> None:
            self._tasks.discard(completed)
            if self._tails.get(conversation_key) is completed:
                self._tails.pop(conversation_key, None)
            if completed.cancelled():
                return
            error = completed.exception()
            if error is not None:
                logger.error(
                    "Comms responder turn failed for conversation %s: %s",
                    conversation_key,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(finish)
        return task

    async def drain(self) -> None:
        """Wait for all currently queued turns."""
        tasks = tuple(self._tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        """Cancel all active and queued turns."""
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class CommunicationsResponder:
    """Gate, route, queue, and deliver inbound responder turns."""

    def __init__(
        self,
        manager: CommunicationsManagerProtocol,
        *,
        backend: ResponderBackend | None = None,
    ) -> None:
        self._manager = manager
        self._backend = backend
        self._turn_queue = ConversationTurnQueue()

    def set_backend(self, backend: ResponderBackend | None) -> None:
        """Install or clear the ChatSession-backed turn implementation."""
        self._backend = backend

    async def handle_event(self, event: str, **kwargs: object) -> None:
        """Consume inbound communications events from the shared event fan-out."""
        if event != "comms.message_received":
            return
        message = kwargs.get("message")
        if not isinstance(message, CommsMessage):
            logger.warning("Ignoring communications event without a CommsMessage")
            return
        await self.handle_message(message)

    async def handle_message(self, message: CommsMessage) -> asyncio.Task[None] | None:
        """Apply policy and route one inbound message."""
        if message.content_type == "reaction":
            logger.debug("Ignoring reaction event %s in responder pipeline", message.id)
            return None
        if not message.content.strip():
            logger.info("Ignoring responder message %s without text content", message.id)
            return None

        channel = self._manager.get_channel(message.channel_id)
        if channel is None:
            logger.warning(
                "Ignoring responder message %s for unknown channel %s",
                message.id,
                message.channel_id,
            )
            return None

        context = self._build_context(channel, message)
        if context is None:
            return None
        if self._backend is None:
            logger.debug(
                "Ignoring responder message %s because no backend is configured",
                message.id,
            )
            return None

        command = _command_name(message.content)
        if command is not None:
            await self._run_command(command, context)
            return None

        conversation_key = f"{channel.id}:{context.conversation_id}"
        return self._turn_queue.enqueue(
            conversation_key,
            lambda: self._run_turn(context),
        )

    async def drain(self) -> None:
        """Wait for all queued responder turns."""
        await self._turn_queue.drain()

    async def stop(self) -> None:
        """Cancel all queued responder turns."""
        await self._turn_queue.stop()

    def _build_context(
        self,
        channel: ChannelConfig,
        message: CommsMessage,
    ) -> ResponderContext | None:
        channel_config = _object_mapping(channel.config_json)
        responder_config = _object_mapping(channel_config.get("responder"))
        if responder_config.get("enabled") is not True:
            return None

        group_decision = evaluate_group_message(channel_config, message)
        sender_id = group_decision.sender_id
        if sender_id is None:
            logger.warning(
                "Ignoring responder message %s without external_user_id metadata",
                message.id,
            )
            return None

        if group_decision.is_group:
            if not group_decision.authorized or not group_decision.should_respond:
                log = logger.debug if group_decision.reason == "mention_required" else logger.info
                log(
                    "Ignoring group message for conversation %s: %s",
                    group_decision.conversation_id,
                    group_decision.reason,
                )
                return None
        else:
            allow_from = _string_set(channel_config.get("allow_from"))
            if sender_id not in allow_from and "*" not in allow_from:
                logger.info(
                    "Ignoring comms message from sender %s outside allow_from on channel %s",
                    sender_id,
                    channel.name,
                )
                return None

        effective_responder = dict(responder_config)
        effective_responder.update(_object_mapping(group_decision.group_config.get("responder")))
        return ResponderContext(
            channel=channel,
            message=message,
            conversation_id=group_decision.conversation_id,
            sender_id=sender_id,
            is_group=group_decision.is_group,
            responder_config=effective_responder,
        )

    async def _run_turn(self, context: ResponderContext) -> None:
        backend = self._backend
        if backend is None:
            return
        configured_reaction = context.responder_config.get("ack_reaction")
        ack_reaction = (
            configured_reaction.strip()
            if isinstance(configured_reaction, str) and configured_reaction.strip()
            else None
        )
        acknowledged = False
        if ack_reaction is not None:
            acknowledged = await self._set_ack_reaction(context, ack_reaction)
        try:
            response = await backend.run_turn(context)
            await self._deliver_response(context, response)
        finally:
            if acknowledged:
                await self._set_ack_reaction(context, None)

    async def _run_command(self, command: str, context: ResponderContext) -> None:
        backend = self._backend
        if backend is None:
            return
        if command == "new":
            response = await backend.new_session(context)
        elif command == "reset":
            response = await backend.reset_session(context)
        elif command == "stop":
            response = await backend.stop_turn(context)
        elif command == "status":
            response = await backend.status(context)
        else:
            response = await backend.help(context)
        await self._deliver_response(context, response)

    async def _deliver_response(
        self,
        context: ResponderContext,
        response: str | None,
    ) -> None:
        if response is None or not response.strip():
            return
        await self._manager.send_message(
            context.channel.name,
            response,
            session_id=context.message.session_id,
            metadata={"platform_destination": context.conversation_id},
        )

    async def _set_ack_reaction(
        self,
        context: ResponderContext,
        reaction: str | None,
    ) -> bool:
        platform_message_id = context.message.platform_message_id
        if not platform_message_id:
            return False
        try:
            await self._manager.set_reaction(
                context.channel.name,
                context.conversation_id,
                platform_message_id,
                reaction,
            )
        except (NotImplementedError, ValueError):
            logger.debug(
                "Channel %s does not support acknowledgement reactions",
                context.channel.name,
            )
            return False
        except Exception:
            logger.warning(
                "Failed to update acknowledgement reaction on channel %s",
                context.channel.name,
                exc_info=True,
            )
            return False
        return True


def _object_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _string_set(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        str(item) for item in value if not isinstance(item, bool) and isinstance(item, str | int)
    }


def _command_name(content: str) -> str | None:
    stripped = content.lstrip()
    if not stripped.startswith("/"):
        return None
    token = stripped.split(maxsplit=1)[0]
    command = token[1:].split("@", maxsplit=1)[0].casefold()
    return command if command in _COMMANDS else None
