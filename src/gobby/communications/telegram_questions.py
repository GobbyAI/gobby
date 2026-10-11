"""Telegram answers for a terminal's concurrent AskUserQuestion permission hook."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.telegram_decisions import edit_keyboard_message

if TYPE_CHECKING:
    from gobby.communications.manager import CommunicationsManager
    from gobby.communications.models import CommsMessage
    from gobby.servers.pending_interactions import PendingInteractionManager

QUESTION_ACTION = "ask_user_question"
QUESTION_TIMEOUT_SECONDS = 110
logger = logging.getLogger(__name__)


@dataclass
class _QuestionPrompt:
    session_id: str
    tool_call_id: str
    interaction_id: str
    pending: PendingInteractionManager
    questions: list[dict[str, Any]]
    answers: dict[str, str] = field(default_factory=dict)
    selected: dict[int, list[str]] = field(default_factory=dict)
    other: int | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    deliveries: list[tuple[str, CommsMessage]] = field(default_factory=list)
    resolved: bool = False


class TelegramQuestionController:
    """Render pending questions; consume answers before ordinary mailbox delivery."""

    def __init__(self, manager: CommunicationsManager) -> None:
        self._manager = manager
        self._prompts: dict[str, _QuestionPrompt] = {}

    async def ask(
        self,
        pending: PendingInteractionManager,
        session_id: str,
        tool_call_id: str,
        tool_input: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Wait alongside the native terminal prompt, returning a hook answer if Telegram wins."""
        channels = []
        for channel in await asyncio.to_thread(self._manager.list_channels):
            if channel.enabled and channel.channel_type == "telegram":
                destination = await asyncio.to_thread(
                    self._manager.attached_destination, channel.id, session_id
                )
                if destination is not None:
                    channels.append((channel, destination))
        questions = _questions(tool_input)
        if not channels or questions is None:
            return None
        interaction_id = await pending.create(
            session_id=session_id,
            kind="ask_user",
            provider="claude",
            payload=tool_input,
            tool_name="AskUserQuestion",
            # ghook waits 115s; the provider's command hook deadline is 120s.
            timeout_seconds=QUESTION_TIMEOUT_SECONDS,
        )
        prompt = _QuestionPrompt(session_id, tool_call_id, interaction_id, pending, questions)
        self._prompts[interaction_id] = prompt
        content, keyboard = _render(prompt)
        resolution = "Telegram answer window closed. Answer in the terminal."
        try:
            for channel, destination in channels:
                kind, _, address = destination.partition(":")
                chat_id, _, thread_id = address.partition(":")
                metadata: dict[str, Any] = {
                    "inline_keyboard": keyboard,
                    "callback_action": QUESTION_ACTION,
                    "callback_ttl_seconds": QUESTION_TIMEOUT_SECONDS,
                    "pending_question_id": interaction_id,
                    "platform_destination": chat_id,
                }
                if kind == "topic":
                    metadata["thread_id"] = thread_id
                sent = await self._manager.send_message(
                    channel.name,
                    content,
                    session_id=session_id,
                    metadata=metadata,
                )
                if sent.status != "sent":
                    raise RuntimeError(f"Question delivery failed: {sent.status}")
                prompt.deliveries.append((channel.name, sent))
            result = await pending.wait(interaction_id)
            response = result.get("response") or {}
            if result.get("decision") != "answer" or response.get("source") != "telegram":
                if response.get("source") == "terminal":
                    resolution = "This question is resolved in the terminal."
                return None
            resolution = "This question is resolved."
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PermissionRequest",
                    "decision": {
                        "behavior": "allow",
                        "updatedInput": {**tool_input, "answers": response["answers"]},
                    },
                }
            }
        finally:
            self._prompts.pop(interaction_id, None)
            async with prompt.lock:
                prompt.resolved = True
                await pending.expire(interaction_id)
                await self._refresh(prompt, resolution=resolution)

    async def terminal_answer(
        self, session_id: str, tool_call_id: str, tool_input: dict[str, Any]
    ) -> None:
        """A completed native tool resolves the same pending interaction."""
        for prompt in tuple(self._prompts.values()):
            matches = (
                prompt.tool_call_id == tool_call_id
                if prompt.tool_call_id
                else prompt.questions == tool_input.get("questions")
            )
            if prompt.session_id == session_id and matches:
                async with prompt.lock:
                    await prompt.pending.resolve(
                        prompt.interaction_id, "answer", {"source": "terminal"}
                    )

    async def handle(self, channel_name: str, message: CommsMessage) -> bool:
        """Consume authenticated callbacks and typed Other replies to our question messages."""
        if message.content_type not in {"text", "callback"}:
            return False
        if (
            not self._prompts
            and message.metadata_json.get("callback_action") != QUESTION_ACTION
            and not message.metadata_json.get("reply_to_message_id")
        ):
            return False
        channel = await asyncio.to_thread(self._manager.get_channel_by_name, channel_name)
        if channel is None or channel.channel_type != "telegram":
            return False
        callback = message.content_type == "callback"
        if callback:
            if message.metadata_json.get("callback_action") != QUESTION_ACTION:
                return False
            if message.metadata_json.get("callback_status") != "ok":
                return True
            try:
                value = json.loads(str(message.metadata_json.get("callback_value", "")))
                interaction_id, question_index, option = value
            except (ValueError, TypeError):
                return True
            prompt = self._prompts.get(interaction_id) if isinstance(interaction_id, str) else None
        else:
            reply_id = message.metadata_json.get("reply_to_message_id")
            chat_id = message.metadata_json.get("chat_id")
            source = (
                await asyncio.to_thread(
                    self._manager.store.get_message_by_platform_id,
                    channel_name,
                    str(reply_id),
                    platform_destination=str(chat_id),
                )
                if reply_id and chat_id
                else None
            )
            interaction_id = source.metadata_json.get("pending_question_id") if source else None
            prompt = self._prompts.get(str(interaction_id)) if interaction_id else None
            if not interaction_id:
                # After Other is tapped, the attached DM's next text is the answer.
                attached = await asyncio.to_thread(
                    self._manager.attached_session, channel.id, f"dm:{chat_id}"
                )
                prompt = next(
                    (
                        item
                        for item in self._prompts.values()
                        if item.session_id == message.session_id
                        and item.other is not None
                        and attached == item.session_id
                    ),
                    None,
                )
                if prompt is None:
                    return False
            question_index = prompt.other if prompt is not None else None
            option = "text"
        if prompt is None or message.session_id != prompt.session_id:
            await self._feedback(channel_name, message, "This question is already resolved.")
            return True
        async with prompt.lock:
            if prompt.resolved:
                await self._feedback(channel_name, message, "This question is already resolved.")
                return True
            if not isinstance(question_index, int) or not 0 <= question_index < len(
                prompt.questions
            ):
                await self._feedback(
                    channel_name, message, "Choose Other on the question to answer."
                )
                return True
            question = prompt.questions[question_index]
            key = question["question"]
            if key in prompt.answers:
                await self._feedback(channel_name, message, "This question is already resolved.")
                return True
            if option == "other":
                prompt.other = question_index
                await self._refresh(prompt)
                await self._feedback(channel_name, message, "Reply with your answer.")
                return True
            if option == "text":
                answer = message.content.strip()
                if not answer:
                    return True
                prompt.answers[key] = answer
                prompt.other = None
            elif option == "done" and question.get("multiSelect"):
                selected = prompt.selected.get(question_index, [])
                if not selected:
                    await self._feedback(channel_name, message, "Select an option first.")
                    return True
                prompt.answers[key] = ", ".join(selected)
            elif isinstance(option, int) and 0 <= option < len(question["options"]):
                label = question["options"][option]["label"]
                if question.get("multiSelect"):
                    selected = prompt.selected.setdefault(question_index, [])
                    if label in selected:
                        selected.remove(label)
                    else:
                        selected.append(label)
                    await self._refresh(prompt)
                    await self._feedback(
                        channel_name, message, "Selected: " + (", ".join(selected) or "none")
                    )
                    return True
                prompt.answers[key] = label
            else:
                return True
            if prompt.other == question_index:
                prompt.other = None
            if len(prompt.answers) == len(prompt.questions):
                accepted = await prompt.pending.resolve(
                    prompt.interaction_id,
                    "answer",
                    {"source": "telegram", "answers": prompt.answers},
                )
                text = "Answered." if accepted else "This question is already resolved."
            else:
                text = "Answer saved. Answer the remaining questions."
                await self._refresh(prompt)
            await self._feedback(channel_name, message, text)
        return True

    async def _refresh(self, prompt: _QuestionPrompt, *, resolution: str | None = None) -> None:
        """Replace one-use tokens after partial answers; retire the keyboard on resolution."""
        content, keyboard = _render(prompt)
        if resolution is not None:
            content += "\n" + resolution
        for channel_name, source in prompt.deliveries:
            adapter = self._manager._adapters.get(channel_name)
            if not isinstance(adapter, TelegramAdapter):
                continue
            destination = source.metadata_json.get("platform_destination")
            if not isinstance(destination, str):
                continue
            try:
                await edit_keyboard_message(
                    self._manager,
                    adapter,
                    source.id,
                    content,
                    destination,
                    None if resolution is not None else keyboard,
                )
            except Exception:
                # Resolution remains authoritative even if Telegram cannot edit;
                # stale taps are consumed as resolved, never delivered as a new turn.
                logger.debug("Could not refresh question message %s", source.id, exc_info=True)

    async def _feedback(self, channel_name: str, message: CommsMessage, text: str) -> None:
        try:
            await self._manager.send_message(
                channel_name,
                text,
                session_id=message.session_id,
                metadata={"platform_destination": message.metadata_json.get("chat_id")},
            )
        except Exception:
            # A feedback outage must not fan this already-consumed answer out to
            # the ordinary responder or wake a second terminal turn.
            logger.debug("Could not acknowledge question answer %s", message.id, exc_info=True)


def _questions(tool_input: dict[str, Any]) -> list[dict[str, Any]] | None:
    questions = tool_input.get("questions")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
        return None
    for question in questions:
        if not isinstance(question, dict) or not isinstance(question.get("question"), str):
            return None
        options = question.get("options")
        if not isinstance(options, list) or not 2 <= len(options) <= 4:
            return None
        if any(
            not isinstance(option, dict)
            or not isinstance(option.get("label"), str)
            or not isinstance(option.get("description", ""), str)
            for option in options
        ):
            return None
    if len({question["question"] for question in questions}) != len(questions):
        return None
    return questions


def _render(prompt: _QuestionPrompt) -> tuple[str, list[list[dict[str, str]]]]:
    lines: list[str] = []
    keyboard: list[list[dict[str, str]]] = []
    for index, question in enumerate(prompt.questions):
        prefix = f"{index + 1}. " if len(prompt.questions) > 1 else ""
        lines.append(prefix + question["question"])
        if question["question"] in prompt.answers:
            lines.append("Answer: " + prompt.answers[question["question"]])
            continue
        row = []
        for option_index, option in enumerate(question["options"]):
            lines.append(f"{option['label']}: {option.get('description', '')}")
            row.append(
                {
                    "text": prefix
                    + ("✓ " if option["label"] in prompt.selected.get(index, []) else "")
                    + option["label"],
                    "value": json.dumps([prompt.interaction_id, index, option_index]),
                }
            )
        row.append(
            {"text": prefix + "Other", "value": json.dumps([prompt.interaction_id, index, "other"])}
        )
        if question.get("multiSelect"):
            row.append(
                {
                    "text": prefix + "Done",
                    "value": json.dumps([prompt.interaction_id, index, "done"]),
                }
            )
        keyboard.append(row)
    lines.append("Choose an option, or choose Other and reply with your answer.")
    return "\n".join(lines), keyboard
