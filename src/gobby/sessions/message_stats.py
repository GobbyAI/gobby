"""Shared transcript message-stats computation.

Single source of truth for the session stat predicate so the live
``SessionMessageProcessor`` poll loop and the batch ``SessionLifecycleManager``
expiry path cannot drift. Both call :func:`compute_message_stats` over parsed
transcript messages; the live path accumulates per-poll batches, the batch path
computes the full transcript at once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, TypedDict

from gobby.sessions.transcripts.base import NON_MESSAGE_CONTENT_TYPES

_LAST_ASSISTANT_CONTENT_LIMIT = 500
TURN_BOUNDARY_CONTENT_TYPE = "turn_completed"
TURN_BOUNDARY_SOURCES: frozenset[str] = frozenset({"grok"})


class MessageStats(TypedDict):
    """Stat counts derived from a list of parsed transcript messages."""

    message_count: int
    turn_count: int
    tool_call_count: int
    last_assistant_content: str | None


class MessageProtocol(Protocol):
    role: str | None
    content_type: str | None
    content: object
    tool_name: str | None
    source: str | None


def compute_message_stats(messages: Sequence[MessageProtocol]) -> MessageStats:
    """Compute session stats from parsed transcript messages.

    The predicate, shared by the live and batch stat writers:

    - ``message_count`` counts every parsed message except session-metadata
      content types (``NON_MESSAGE_CONTENT_TYPES``: native titles, hook prompts,
      usage, turn boundaries, and the unmodeled-record sentinel).
    - ``turn_count`` counts one completed turn per ``turn_completed`` boundary,
      plus assistant ``text`` messages whose ``source`` is not in
      ``TURN_BOUNDARY_SOURCES`` (those sources emit explicit boundaries).
    - ``tool_call_count`` counts messages carrying a truthy ``tool_name``.
    - ``last_assistant_content`` is the last non-empty assistant text, stripped
      and clamped to the trailing ``500`` characters; ``None`` when the batch
      holds no such message.
    """
    message_count = 0
    turn_count = 0
    tool_call_count = 0
    last_assistant_content: str | None = None

    for msg in messages:
        content_type = _message_attr(msg, "content_type")
        if content_type == TURN_BOUNDARY_CONTENT_TYPE:
            # Explicit turn boundary: counts one turn, is not a conversation message.
            turn_count += 1
            continue
        if content_type in NON_MESSAGE_CONTENT_TYPES:
            # Session metadata (native titles, unmodeled-record sentinel) is not
            # a conversation message — never counted.
            continue
        message_count += 1
        role = _message_attr(msg, "role")
        if role == "assistant" and content_type == "text":
            if _message_attr(msg, "source") not in TURN_BOUNDARY_SOURCES:
                turn_count += 1
            content = _message_attr(msg, "content")
            if isinstance(content, str) and content.strip():
                last_assistant_content = content.strip()[-_LAST_ASSISTANT_CONTENT_LIMIT:]
        if _message_attr(msg, "tool_name"):
            tool_call_count += 1

    return MessageStats(
        message_count=message_count,
        turn_count=turn_count,
        tool_call_count=tool_call_count,
        last_assistant_content=last_assistant_content,
    )


def _message_attr(msg: MessageProtocol, name: str) -> Any:
    try:
        return getattr(msg, name)
    except AttributeError:
        return None


def empty_message_stats() -> MessageStats:
    """Return a fresh zero-value stats accumulator."""
    return MessageStats(
        message_count=0,
        turn_count=0,
        tool_call_count=0,
        last_assistant_content=None,
    )


def _coerce_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, count)


def merge_message_stats(
    current: Mapping[str, Any] | None,
    batch: MessageStats,
) -> MessageStats:
    """Fold a batch stats block into an existing accumulator."""
    stats = empty_message_stats()
    if current is not None:
        stats["message_count"] = _coerce_count(current.get("message_count"))
        stats["turn_count"] = _coerce_count(current.get("turn_count"))
        stats["tool_call_count"] = _coerce_count(current.get("tool_call_count"))
        last_assistant = current.get("last_assistant_content")
        if isinstance(last_assistant, str):
            stats["last_assistant_content"] = last_assistant

    stats["message_count"] += batch["message_count"]
    stats["turn_count"] += batch["turn_count"]
    stats["tool_call_count"] += batch["tool_call_count"]
    if batch["last_assistant_content"] is not None:
        stats["last_assistant_content"] = batch["last_assistant_content"]
    return stats


def accumulate_message_stats(
    current: Mapping[str, Any] | None,
    messages: Sequence[MessageProtocol],
) -> MessageStats:
    """Compute and merge stats for a parsed-message batch."""
    return merge_message_stats(current, compute_message_stats(messages))
