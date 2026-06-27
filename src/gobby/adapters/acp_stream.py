"""ACP stream event normalization helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StreamEvent:
    """A normalized event from the provider ACP stream.

    Attributes:
        event_type: One of "init", "content_delta", "result", "error".
        data: Event-specific payload.
    """

    event_type: str
    data: dict[str, Any] = field(default_factory=dict)


def normalize_notification(
    raw: dict[str, Any],
    *,
    extract_text_content: Callable[[Any], str] | None = None,
) -> StreamEvent:
    """Normalize a JSON-RPC notification to a StreamEvent."""
    text_extractor = extract_text_content or extract_text
    method = raw.get("method", "")
    params = raw.get("params", {})
    if not isinstance(params, dict):
        params = {}

    if method == "session/init" or method == "init":
        return StreamEvent(
            event_type="init",
            data=params,
        )

    if method == "session/message" or method == "message":
        role = params.get("role", "")
        is_delta = params.get("delta", False)
        content = params.get("content", "")

        if role == "assistant" and is_delta:
            return StreamEvent(
                event_type="content_delta",
                data={"content": content, "role": role},
            )

        return StreamEvent(
            event_type="message",
            data=params,
        )

    if method == "session/update":
        update = params.get("update", {})
        if not isinstance(update, dict):
            return StreamEvent(event_type="session/update", data=params or raw)

        update_type = update.get("sessionUpdate", "")
        content = update.get("content")
        text = text_extractor(content)

        if update_type == "agent_message_chunk":
            return StreamEvent(
                event_type="content_delta",
                data={
                    "content": text,
                    "role": "assistant",
                    "message_id": update.get("messageId"),
                },
            )

        if update_type == "agent_thought_chunk":
            return StreamEvent(
                event_type="thinking_delta",
                data={
                    "content": text,
                    "message_id": update.get("messageId"),
                },
            )

        if update_type == "user_message_chunk":
            return StreamEvent(
                event_type="message",
                data={
                    "role": "user",
                    "content": text,
                    "message_id": update.get("messageId"),
                },
            )

        if update_type == "tool_call":
            tool_input = {}
            for input_key in ("rawInput", "input"):
                if input_key in update:
                    value = update[input_key]
                    tool_input = value if isinstance(value, dict) else {}
                    break
            return StreamEvent(
                event_type="tool_call",
                data={
                    "call_id": update.get("toolCallId"),
                    "tool_name": update.get("title") or update.get("name"),
                    "tool_input": tool_input,
                },
            )

        return StreamEvent(event_type=update_type or method, data=update)

    if method == "session/result" or method == "result":
        return StreamEvent(
            event_type="result",
            data={"stats": params.get("stats", params)},
        )

    if method == "session/error" or method == "error":
        return StreamEvent(
            event_type="error",
            data={
                "message": params.get("message", "Unknown error"),
                "code": params.get("code"),
            },
        )

    return StreamEvent(event_type=method or "unknown", data=params or raw)


def extract_text(content: Any) -> str:
    """Extract text from ACP content payloads."""
    if isinstance(content, str):
        return content

    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        if "text" in content:
            return str(content.get("text", ""))
        if "content" in content:
            return str(content.get("content", ""))
        return ""

    if isinstance(content, list):
        parts = [extract_text(item) for item in content]
        return "".join(part for part in parts if part)

    return ""
