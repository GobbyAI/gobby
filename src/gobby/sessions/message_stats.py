"""Shared transcript message-stats computation.

Single source of truth for the session stat predicate so the live
``SessionMessageProcessor`` poll loop and the batch ``SessionLifecycleManager``
expiry path cannot drift. Both call :func:`compute_message_stats` over parsed
transcript messages; the live path accumulates per-poll batches, the batch path
computes the full transcript at once.
"""

from __future__ import annotations

from typing import Any, TypedDict

_LAST_ASSISTANT_CONTENT_LIMIT = 500


class MessageStats(TypedDict):
    """Stat counts derived from a list of parsed transcript messages."""

    message_count: int
    turn_count: int
    tool_call_count: int
    last_assistant_content: str | None


def compute_message_stats(messages: list[Any]) -> MessageStats:
    """Compute session stats from parsed transcript messages.

    The predicate, shared by the live and batch stat writers:

    - ``message_count`` counts every parsed message.
    - ``turn_count`` counts assistant messages whose ``content_type`` is
      ``"text"`` (one completed assistant text turn each).
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
        message_count += 1
        if getattr(msg, "role", None) == "assistant" and getattr(msg, "content_type", None) == "text":
            turn_count += 1
            content = getattr(msg, "content", None)
            if isinstance(content, str) and content.strip():
                last_assistant_content = content.strip()[-_LAST_ASSISTANT_CONTENT_LIMIT:]
        if getattr(msg, "tool_name", None):
            tool_call_count += 1

    return MessageStats(
        message_count=message_count,
        turn_count=turn_count,
        tool_call_count=tool_call_count,
        last_assistant_content=last_assistant_content,
    )
