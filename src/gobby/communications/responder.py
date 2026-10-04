"""Inbound communications responder pipeline."""

from __future__ import annotations

import asyncio
import itertools
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

from gobby.communications.group_policy import evaluate_group_message
from gobby.communications.models import AnswerOutcome, ChannelConfig, CommsMessage

logger = logging.getLogger(__name__)

_COMMANDS = frozenset({"new", "reset", "stop", "status", "help"})
_MAX_PENDING_TURNS_PER_CONVERSATION = 8
_BUSY_RESPONSE = "This conversation is busy. Try again after a pending response finishes."
# Backoff for decision-answer work that never started (queue full, transient storage
# failure); the last delay repeats until the work is claimed or no longer pending.
_ANSWER_RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0)
_RECOVERY_RETRY_KEY = "decision-answer-recovery"
_STATUS_GRACE_SECONDS = 5.0


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


class DecisionAnswerLedger(Protocol):
    """Durable delivery state of decision answers the responder owes comms sessions."""

    def get_answer(self, answer_id: str) -> CommsMessage | None: ...

    def claim_answer(self, answer_id: str, attempt: int, epoch: str) -> bool: ...

    def block_answer(self, answer_id: str, attempt: int) -> CommsMessage | None: ...

    def settle_answer(
        self, answer_id: str, attempt: int, outcome: AnswerOutcome
    ) -> CommsMessage | None: ...

    def sweep_in_doubt_answers(self, epoch: str) -> list[CommsMessage]: ...

    def list_pending_answers(self) -> list[CommsMessage]: ...

    def list_unshown_statuses(self) -> list[CommsMessage]: ...


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
        self._pending_counts: dict[str, int] = {}

    def enqueue(
        self,
        conversation_key: str,
        callback: Callable[[], Awaitable[None]],
    ) -> asyncio.Task[None] | None:
        pending_count = self._pending_counts.get(conversation_key, 0)
        if pending_count >= _MAX_PENDING_TURNS_PER_CONVERSATION:
            return None
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
        self._pending_counts[conversation_key] = pending_count + 1

        def finish(completed: asyncio.Task[None]) -> None:
            self._tasks.discard(completed)
            remaining = self._pending_counts.get(conversation_key, 1) - 1
            if remaining > 0:
                self._pending_counts[conversation_key] = remaining
            else:
                self._pending_counts.pop(conversation_key, None)
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
        answers: DecisionAnswerLedger | None = None,
        daemon_epoch: str | None = None,
        answer_status: Callable[[CommsMessage], Awaitable[None]] | None = None,
        retry_delays: Sequence[float] = _ANSWER_RETRY_DELAYS,
    ) -> None:
        self._manager = manager
        self._backend = backend
        self._answers = answers
        # Identifies this daemon's claims; a later daemon marks them in doubt.
        self._epoch = daemon_epoch or str(uuid.uuid4())
        self._answer_status = answer_status
        self._retry_delays = tuple(retry_delays) or _ANSWER_RETRY_DELAYS
        self._retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._status_tasks: set[asyncio.Task[None]] = set()
        self._recovered = False
        self._stopping = False
        self._turn_queue = ConversationTurnQueue()

    def set_backend(self, backend: ResponderBackend | None) -> None:
        """Install or clear the ChatSession-backed turn implementation.

        Installing one after startup recovery routes answers that waited for it.
        """
        self._backend = backend
        if backend is not None and self._recovered:
            self._retry_soon(_RECOVERY_RETRY_KEY, self._recover_once, delay_first=False)

    async def handle_event(self, event: str, **kwargs: object) -> None:
        """Consume inbound communications events from the shared event fan-out."""
        if event != "comms.message_received":
            return
        message = kwargs.get("message")
        if not isinstance(message, CommsMessage):
            logger.warning("Ignoring communications event without a CommsMessage")
            return
        await self.handle_message(message)

    def will_respond(self, message: CommsMessage) -> bool:
        """Whether ``handle_message`` would answer this ordinary message with a turn or command."""
        if (
            self._backend is None
            or message.content_type == "reaction"
            or message.answer_delivery is not None
            or not message.content.strip()
        ):
            return False
        channel = self._manager.get_channel(message.channel_id)
        return channel is not None and self._build_context(channel, message) is not None

    async def handle_message(self, message: CommsMessage) -> asyncio.Task[None] | None:
        """Apply policy and route one inbound message."""
        if message.content_type == "reaction":
            logger.debug("Ignoring reaction event %s in responder pipeline", message.id)
            return None
        if message.answer_delivery == "mailbox":
            logger.debug(
                "Ignoring decision answer %s delivered by its session's mailbox", message.id
            )
            return None
        if not message.content.strip():
            logger.info("Ignoring responder message %s without text content", message.id)
            return None

        channel = self._manager.get_channel(message.channel_id)
        if message.answer_delivery == "responder":
            context = self._build_context(channel, message) if channel is not None else None
            return await self._route_answer(message, context)
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
        task = self._turn_queue.enqueue(conversation_key, lambda: self._run_turn(context))
        if task is None:
            await self._deliver_response(context, _BUSY_RESPONSE)
        return task

    async def recover_decision_answers(self) -> None:
        """Settle decision answers a previous daemon left: show in-doubt, route pending.

        A storage failure retries with backoff instead of waiting for another restart.
        """
        if self._answers is None:
            return
        self._recovered = True
        if not await self._recover_once():
            self._retry_soon(_RECOVERY_RETRY_KEY, self._recover_once)

    async def _recover_once(self) -> bool:
        answers = self._answers
        if answers is None:
            return True
        try:
            await asyncio.to_thread(answers.sweep_in_doubt_answers, self._epoch)
            unshown = await asyncio.to_thread(answers.list_unshown_statuses)
            pending = await asyncio.to_thread(answers.list_pending_answers)
        except Exception:
            logger.warning("Decision answer recovery failed; retrying", exc_info=True)
            return False
        for answer in unshown:
            await self._show_status(answer)
        for message in pending:
            await self.handle_message(message)
        return True

    async def _route_answer(
        self, message: CommsMessage, context: ResponderContext | None
    ) -> asyncio.Task[None] | None:
        # A decision answer is always an answer turn, even when its value looks like a
        # /command. The click passed channel access policy when it was accepted; delivery
        # re-applies current policy, so a revocation since then blocks it visibly.
        answers = self._answers
        if answers is None or self._backend is None:
            logger.warning(
                "Decision answer %s stays pending until a responder backend is installed",
                message.id,
            )
            return None
        if context is None:
            try:
                blocked = await asyncio.to_thread(
                    answers.block_answer, message.id, message.answer_attempt
                )
            except Exception:
                logger.warning("Could not block decision answer %s", message.id, exc_info=True)
                self._retry_answer_soon(message.id)
                return None
            if blocked is not None:
                logger.warning(
                    "Decision answer %s for decision %s blocked by current responder policy "
                    "on channel %s",
                    message.id,
                    message.metadata_json.get("callback_decision_id"),
                    message.channel_id,
                )
                await self._show_status(blocked)
            return None
        conversation_key = f"{context.channel.id}:{context.conversation_id}"
        task = self._turn_queue.enqueue(
            conversation_key, lambda: self._run_answer_turn(context, answers)
        )
        if task is None:
            logger.warning(
                "Decision answer %s waits for busy conversation %s", message.id, conversation_key
            )
            self._retry_answer_soon(message.id)
        return task

    async def _run_answer_turn(
        self, context: ResponderContext, answers: DecisionAnswerLedger
    ) -> None:
        # Recovery, a retry and a live event can all queue one attempt; only the claim's
        # winner runs it. The claim precedes the turn's effects, so an attempt that fails
        # or is interrupted is never rerun without a person's Retry answer click.
        message = context.message
        attempt = message.answer_attempt
        try:
            claimed = await asyncio.to_thread(
                answers.claim_answer, message.id, attempt, self._epoch
            )
        except Exception:
            logger.warning("Could not claim decision answer %s", message.id, exc_info=True)
            self._retry_answer_soon(message.id)
            return
        if not claimed:
            return
        if attempt > 1:
            context = replace(context, message=replace(message, content=_retry_content(message)))
        try:
            await self._run_turn(context)
        except asyncio.CancelledError:
            await self._settle_answer(answers, message, "in_doubt", cancelled=True)
            raise
        except Exception:
            logger.exception(
                "Decision answer %s for decision %s failed its turn; it may have partly acted",
                message.id,
                message.metadata_json.get("callback_decision_id"),
            )
            await self._settle_answer(answers, message, "failed")
            return
        await self._settle_answer(answers, message, "delivered")

    async def _settle_answer(
        self,
        answers: DecisionAnswerLedger,
        message: CommsMessage,
        outcome: AnswerOutcome,
        *,
        cancelled: bool = False,
    ) -> None:
        write = asyncio.to_thread(
            answers.settle_answer, message.id, message.answer_attempt, outcome
        )
        try:
            settled = await (asyncio.shield(write) if cancelled else write)
        except Exception:
            # The attempt stays ``started``; the next daemon start marks it in doubt.
            logger.exception("Could not record decision answer %s as %s", message.id, outcome)
            return
        if settled is None:
            return
        if not cancelled:
            await self._show_status(settled)
            return
        # A cancelled turn must not wait on Telegram; startup republishes a lost status.
        task = asyncio.create_task(self._show_status(settled))
        self._status_tasks.add(task)
        task.add_done_callback(self._status_tasks.discard)

    async def _show_status(self, answer: CommsMessage) -> None:
        """Show an answer's status, retrying with backoff until it reaches Telegram."""
        if self._answer_status is None or answer.answer_status_key is None:
            return
        if not await self._publish_status(answer):
            self._retry_soon(f"status:{answer.id}", lambda: self._republish_status(answer.id))

    async def _publish_status(self, answer: CommsMessage) -> bool:
        """Publish once; True when the answer's current status is recorded as shown."""
        if self._answer_status is None:
            return True
        try:
            await self._answer_status(answer)
        except Exception:
            logger.warning(
                "Could not show decision answer %s status; retrying", answer.id, exc_info=True
            )
            return False
        return await self._republish_status(answer.id, publish=False)

    async def _republish_status(self, answer_id: str, *, publish: bool = True) -> bool:
        """True once the answer's current status is shown; else publish it when asked.

        Only the status is published; the answer's turn is never rerun here.
        """
        answers = self._answers
        if answers is None:
            return True
        try:
            answer = await asyncio.to_thread(answers.get_answer, answer_id)
        except Exception:
            logger.debug("Could not reload decision answer %s", answer_id, exc_info=True)
            return False
        if answer is None or answer.answer_status_key in {
            None,
            answer.metadata_json.get("answer_status_shown"),
        }:
            return True
        return publish and await self._publish_status(answer)

    def _retry_answer_soon(self, answer_id: str) -> None:
        self._retry_soon(answer_id, lambda: self._retry_pending_answer(answer_id))

    async def _retry_pending_answer(self, answer_id: str) -> bool:
        """Route an answer no turn has claimed; True once it is claimed or settled."""
        answers = self._answers
        if answers is None or self._backend is None:
            return True  # set_backend reroutes pending answers
        try:
            answer = await asyncio.to_thread(answers.get_answer, answer_id)
        except Exception:
            logger.debug("Could not reload decision answer %s", answer_id, exc_info=True)
            return False
        if answer is None or answer.answer_outcome != "pending":
            return True
        task = await self.handle_message(answer)
        if task is None:
            return False
        await asyncio.gather(task, return_exceptions=True)
        try:
            answer = await asyncio.to_thread(answers.get_answer, answer_id)
        except Exception:
            return False
        return answer is None or answer.answer_outcome != "pending"

    def _retry_soon(
        self, key: str, attempt: Callable[[], Awaitable[bool]], *, delay_first: bool = True
    ) -> None:
        if self._stopping or key in self._retry_tasks:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return

        async def retry() -> None:
            delays = itertools.chain(self._retry_delays, itertools.repeat(self._retry_delays[-1]))
            first = True
            for delay in delays:
                if delay_first or not first:
                    await asyncio.sleep(delay)
                first = False
                if await attempt():
                    return

        task = asyncio.create_task(retry(), name=f"comms-answer-retry:{key}")
        self._retry_tasks[key] = task

        def finish(done: asyncio.Task[None]) -> None:
            if self._retry_tasks.get(key) is done:
                self._retry_tasks.pop(key)

        task.add_done_callback(finish)

    async def drain(self) -> None:
        """Wait for all queued responder turns."""
        await self._turn_queue.drain()

    async def stop(self) -> None:
        """Cancel pending answer retries and all queued responder turns.

        Answers whose turns this cancels are in doubt; their status gets a bounded
        chance to reach Telegram, and startup republishes any that did not.
        """
        self._stopping = True
        retries = tuple(self._retry_tasks.values())
        for task in retries:
            task.cancel()
        await asyncio.gather(*retries, return_exceptions=True)
        await self._turn_queue.stop()
        statuses = tuple(self._status_tasks)
        if statuses:
            _, unfinished = await asyncio.wait(statuses, timeout=_STATUS_GRACE_SECONDS)
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*unfinished, return_exceptions=True)

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


def _retry_content(message: CommsMessage) -> str:
    return (
        f"[Retry of a Telegram decision answer, attempt {message.answer_attempt}. An earlier "
        "attempt may have partly run; check what already happened before repeating actions.]\n"
        f"{message.content}"
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


def _command_name(content: str) -> str | None:
    stripped = content.lstrip()
    if not stripped.startswith("/"):
        return None
    token = stripped.split(maxsplit=1)[0]
    command = token[1:].split("@", maxsplit=1)[0].casefold()
    return command if command in _COMMANDS else None
