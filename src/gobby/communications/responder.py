"""Inbound communications responder pipeline."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from gobby.communications.models import ChannelConfig, CommsMessage

logger = logging.getLogger(__name__)

_COMMANDS = frozenset({"new", "reset", "stop", "status", "help"})
_DIRECT_CONVERSATION_TYPES = frozenset({"direct", "dm", "im", "private"})
_GROUP_CONVERSATION_TYPES = frozenset({"channel", "group", "mpim", "room", "supergroup"})


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
                    pass
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

        sender_id = _sender_id(message)
        if sender_id is None:
            logger.warning(
                "Ignoring responder message %s without external_user_id metadata",
                message.id,
            )
            return None

        conversation_id = _conversation_id(message)
        is_group = _is_group_message(message)
        group_config = _group_config(channel_config, conversation_id) if is_group else {}
        if not self._access_allowed(
            channel=channel,
            channel_config=channel_config,
            group_config=group_config,
            sender_id=sender_id,
            conversation_id=conversation_id,
            is_group=is_group,
            mentioned=_is_mentioned(message),
        ):
            return None

        effective_responder = dict(responder_config)
        effective_responder.update(_object_mapping(group_config.get("responder")))
        return ResponderContext(
            channel=channel,
            message=message,
            conversation_id=conversation_id,
            sender_id=sender_id,
            is_group=is_group,
            responder_config=effective_responder,
        )

    def _access_allowed(
        self,
        *,
        channel: ChannelConfig,
        channel_config: Mapping[str, object],
        group_config: Mapping[str, object],
        sender_id: str,
        conversation_id: str,
        is_group: bool,
        mentioned: bool,
    ) -> bool:
        allow_from_value = (
            group_config["allow_from"]
            if "allow_from" in group_config
            else channel_config.get("allow_from")
        )
        allow_from = _string_set(allow_from_value)

        if not is_group:
            if sender_id in allow_from or "*" in allow_from:
                return True
            logger.info(
                "Ignoring comms message from sender %s outside allow_from on channel %s",
                sender_id,
                channel.name,
            )
            return False

        policy_value = group_config.get(
            "group_policy",
            channel_config.get("group_policy", "allowlist"),
        )
        policy = policy_value.casefold() if isinstance(policy_value, str) else "disabled"
        if policy not in {"allowlist", "open"}:
            logger.info(
                "Ignoring group message for conversation %s under group_policy=%s",
                conversation_id,
                policy,
            )
            return False

        groups = _object_mapping(channel_config.get("groups"))
        group_is_configured = conversation_id in groups or "*" in groups
        if groups and not group_is_configured:
            logger.info(
                "Ignoring message from group %s outside configured groups on channel %s",
                conversation_id,
                channel.name,
            )
            return False
        if policy == "allowlist" and not group_is_configured:
            logger.info(
                "Ignoring message from group %s under allowlist group policy",
                conversation_id,
            )
            return False
        if policy == "allowlist" and sender_id not in allow_from and "*" not in allow_from:
            logger.info(
                "Ignoring group message from sender %s outside allow_from on channel %s",
                sender_id,
                channel.name,
            )
            return False

        require_mention_value = group_config.get(
            "require_mention",
            channel_config.get("require_mention", True),
        )
        require_mention = require_mention_value if isinstance(require_mention_value, bool) else True
        if require_mention and not mentioned:
            logger.info(
                "Ignoring unmentioned group message for conversation %s",
                conversation_id,
            )
            return False
        return True

    async def _run_turn(self, context: ResponderContext) -> None:
        backend = self._backend
        if backend is None:
            return
        response = await backend.run_turn(context)
        await self._deliver_response(context, response)

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


def _sender_id(message: CommsMessage) -> str | None:
    raw_sender = message.metadata_json.get("external_user_id")
    if not isinstance(raw_sender, bool) and isinstance(raw_sender, str | int):
        return str(raw_sender)
    return None


def _conversation_id(message: CommsMessage) -> str:
    for key in ("platform_channel_id", "chat_id"):
        value = message.metadata_json.get(key)
        if not isinstance(value, bool) and isinstance(value, str | int) and str(value):
            return str(value)
    conversation_reference = _object_mapping(message.metadata_json.get("conversation_reference"))
    reference_id = conversation_reference.get("conversation_id")
    if (
        not isinstance(reference_id, bool)
        and isinstance(reference_id, str | int)
        and str(reference_id)
    ):
        return str(reference_id)
    return message.session_id or message.id


def _group_config(
    channel_config: Mapping[str, object],
    conversation_id: str,
) -> dict[str, object]:
    groups = _object_mapping(channel_config.get("groups"))
    wildcard = _object_mapping(groups.get("*"))
    explicit = _object_mapping(groups.get(conversation_id))
    return {**wildcard, **explicit}


def _is_group_message(message: CommsMessage) -> bool:
    raw_is_group = message.metadata_json.get("is_group")
    if isinstance(raw_is_group, bool):
        return raw_is_group
    for key in ("conversation_type", "chat_type", "channel_type"):
        value = message.metadata_json.get(key)
        if not isinstance(value, str):
            continue
        normalized = value.casefold()
        if normalized in _GROUP_CONVERSATION_TYPES:
            return True
        if normalized in _DIRECT_CONVERSATION_TYPES:
            return False
    return False


def _is_mentioned(message: CommsMessage) -> bool:
    for key in ("mentioned", "is_mentioned"):
        value = message.metadata_json.get(key)
        if isinstance(value, bool):
            return value
    return False


def _command_name(content: str) -> str | None:
    stripped = content.lstrip()
    if not stripped.startswith("/"):
        return None
    token = stripped.split(maxsplit=1)[0]
    command = token[1:].split("@", maxsplit=1)[0].casefold()
    return command if command in _COMMANDS else None
