"""Lossless, bounded rendering for pending inter-session messages."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from gobby.storage.sessions import system_session_id

TELEGRAM_MESSAGE_TYPE = "telegram_message"
PENDING_MESSAGE_CONTEXT_BUDGET = 6_500
PENDING_MESSAGE_INLINE_LIMIT = 2_000

SenderLabelResolver = Callable[[str | None], str]


@dataclass(frozen=True)
class PendingMessageRenderResult:
    """Rendered context plus the exact delivery boundary it represents."""

    context: str | None
    represented_message_ids: tuple[str, ...]
    deferred_message_ids: tuple[str, ...]


def message_metadata(message: Any) -> dict[str, Any]:
    """Parse optional JSON metadata from an inter-session message."""
    raw = getattr(message, "metadata_json", None)
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _is_self_origin(message: Any) -> bool:
    from_session = getattr(message, "from_session", None)
    return bool(from_session and from_session == getattr(message, "to_session", None))


def render_pending_messages(
    messages: Sequence[Any],
    *,
    resolve_sender: SenderLabelResolver,
    aggregate_budget: int = PENDING_MESSAGE_CONTEXT_BUDGET,
    inline_limit: int = PENDING_MESSAGE_INLINE_LIMIT,
) -> PendingMessageRenderResult:
    """Render complete messages or durable references without slicing content."""
    context = ""
    represented: list[str] = []
    deferred: list[str] = []
    previous_type: str | None = None

    for index, message in enumerate(messages):
        message_id = str(getattr(message, "id", "") or "")
        message_type = str(getattr(message, "message_type", "message") or "message")
        content = str(getattr(message, "content", ""))
        line = _render_message_line(
            message,
            message_id=message_id,
            message_type=message_type,
            content=content,
            resolve_sender=resolve_sender,
            inline_limit=inline_limit,
        )
        if previous_type == message_type:
            addition = f"\n{line}"
        else:
            separator = "\n\n" if context else ""
            addition = f"{separator}{message_group_header(message_type)}\n{line}"

        if len(context) + len(addition) > aggregate_budget:
            deferred.extend(str(getattr(item, "id", "") or "") for item in messages[index:])
            break

        context += addition
        represented.append(message_id)
        previous_type = message_type

    return PendingMessageRenderResult(
        context=context or None,
        represented_message_ids=tuple(represented),
        deferred_message_ids=tuple(deferred),
    )


def message_group_header(message_type: str) -> str:
    """Return the context header for a message type group."""
    if message_type == "web_chat":
        return "[Pending messages from web chat user]:"
    if message_type == "command_result":
        return "[Pending command results]:"
    if message_type == "completion_notification":
        return "[Pending completion notifications]:"
    if message_type == TELEGRAM_MESSAGE_TYPE:
        return "[Pending Telegram messages]:"
    return "[Pending P2P messages from other sessions]:"


def _telegram_metadata(message: Any) -> dict[str, Any] | None:
    """Metadata of Telegram ingress the daemon delivered; None for anything a session sent."""
    if str(getattr(message, "message_type", "")) != TELEGRAM_MESSAGE_TYPE:
        return None
    if getattr(message, "from_session", None) != system_session_id():
        return None
    return message_metadata(message)


def _telegram_sender(metadata: Mapping[str, Any]) -> str:
    username = metadata.get("sender_username")
    return f"Telegram @{username}: " if isinstance(username, str) and username else "Telegram: "


def _telegram_reply_guidance(metadata: Mapping[str, Any]) -> str:
    # Set only by daemon ingress for the enrolled operator's attached conversation.
    if metadata.get("operator_comms") is True:
        return (
            " This session is attached to this Telegram conversation: type your reply "
            "normally and it is mirrored there automatically. Do not also send it with "
            "gobby-agents:send_message."
        )
    channel = metadata.get("channel")
    target = f'(channel="{channel}")' if isinstance(channel, str) and channel else ""
    return (
        " This session is not attached to this Telegram conversation, so typed text is "
        f"not mirrored there; reply with gobby-communications:send_message{target}."
    )


def _render_message_line(
    message: Any,
    *,
    message_id: str,
    message_type: str,
    content: str,
    resolve_sender: SenderLabelResolver,
    inline_limit: int,
) -> str:
    priority = str(getattr(message, "priority", "normal") or "normal")
    priority_label = _priority_label(priority)
    telegram = _telegram_metadata(message)
    if telegram is not None:
        sender = _telegram_sender(telegram)
    elif _is_self_origin(message):
        sender = ""
    else:
        sender = resolve_sender(getattr(message, "from_session", None))
    guidance = _telegram_reply_guidance(telegram) if telegram is not None else ""
    if len(content) <= inline_limit:
        suffix = _message_context_suffix(
            message, message_type, message_id, attribute_sender=telegram is None
        )
        return f"- {priority_label}{sender}{content}{suffix}{guidance}"

    reference = (
        f"{len(content):,}-character message; retrieve with "
        f'gobby-agents:get_inter_session_message(message_id="{message_id}").'
    )
    return f"- {priority_label}{sender}{reference}{guidance}"


def _priority_label(priority: str) -> str:
    if priority == "normal":
        return ""
    if priority == "urgent":
        return "[URGENT] "
    return f"[PRIORITY: {priority.upper()}] "


def _message_context_suffix(
    message: Any, message_type: str, message_id: str, *, attribute_sender: bool = True
) -> str:
    metadata = message_metadata(message)
    bits: list[str] = [f"message_id={message_id}", f"type={message_type}"]
    from_session = getattr(message, "from_session", None)
    # Telegram ingress comes from the daemon's sender, which is not a reply address.
    if attribute_sender and from_session and not _is_self_origin(message):
        bits.append(f"from_session={from_session}")
    for key in ("run_id", "task_id", "completion_id"):
        value = metadata.get(key)
        if value:
            bits.append(f"{key}={value}")
    attachments = metadata.get("attachments")
    if isinstance(attachments, list):
        for attachment in attachments:
            if not isinstance(attachment, Mapping):
                continue
            local_path = attachment.get("local_path")
            if isinstance(local_path, str) and local_path:
                bits.append(f"attachment={local_path}")
    if metadata.get("signoff_message") or metadata.get("signoff"):
        bits.append("signoff=true")
    return f" ({', '.join(bits)})"
