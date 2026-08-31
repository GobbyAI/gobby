"""Lossless, bounded rendering for pending inter-session messages."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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
    return "[Pending P2P messages from other sessions]:"


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
    sender = (
        "" if _is_self_origin(message) else resolve_sender(getattr(message, "from_session", None))
    )
    if len(content) <= inline_limit:
        suffix = _message_context_suffix(message, message_type)
        return f"- {priority_label}{sender}{content}{suffix}"

    reference = (
        f"{len(content):,}-character message; retrieve with "
        f'gobby-agents.get_inter_session_message(message_id="{message_id}").'
    )
    return f"- {priority_label}{sender}{reference}"


def _priority_label(priority: str) -> str:
    if priority == "normal":
        return ""
    if priority == "urgent":
        return "[URGENT] "
    return f"[PRIORITY: {priority.upper()}] "


def _message_context_suffix(message: Any, message_type: str) -> str:
    metadata = message_metadata(message)
    bits: list[str] = [f"type={message_type}"]
    from_session = getattr(message, "from_session", None)
    if from_session and not _is_self_origin(message):
        bits.append(f"from_session={from_session}")
    for key in ("run_id", "task_id", "completion_id"):
        value = metadata.get(key)
        if value:
            bits.append(f"{key}={value}")
    if metadata.get("signoff_message") or metadata.get("signoff"):
        bits.append("signoff=true")
    return f" ({', '.join(bits)})"
