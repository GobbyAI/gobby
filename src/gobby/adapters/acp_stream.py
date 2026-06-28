"""ACP stream event normalization helpers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from gobby.adapters.acp_content import (
    extract_text as _extract_content_text,
)
from gobby.adapters.acp_content import (
    normalize_acp_content_blocks,
    normalize_tool_call_update,
)


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
            content_blocks = normalize_acp_content_blocks(content, include_text=False)
            return StreamEvent(
                event_type="content_delta",
                data={
                    "content": text,
                    "role": "assistant",
                    "message_id": update.get("messageId"),
                    "content_blocks": content_blocks,
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

        if update_type == "plan":
            return StreamEvent(event_type="plan_update", data=update)

        if update_type == "current_mode_update":
            data = dict(update)
            data["current_mode_id"] = update.get("currentModeId")
            return StreamEvent(event_type="current_mode_update", data=data)

        if update_type == "session_info_update":
            data = dict(update)
            session_info = update.get("sessionInfo")
            data["session_info"] = session_info if isinstance(session_info, dict) else {}
            return StreamEvent(event_type="session_info_update", data=data)

        if update_type == "usage_update":
            return StreamEvent(event_type="usage_update", data=dict(update))

        if update_type == "available_commands_update":
            data = dict(update)
            if "commands" not in data and "availableCommands" in data:
                data["commands"] = data.get("availableCommands")
            return StreamEvent(event_type="available_commands_update", data=data)

        if update_type == "tool_call":
            return StreamEvent(
                event_type="tool_call",
                data=normalize_tool_call_update(update),
            )

        if update_type == "tool_call_update":
            data = normalize_tool_call_update(update)
            if data.get("tool_status") in {"completed", "error"}:
                raw_output = data.get("raw_output")
                result = None
                if raw_output is not None:
                    result = {
                        "kind": "json",
                        "content": raw_output,
                        "truncated": False,
                    }
                return StreamEvent(
                    event_type="tool_result",
                    data={
                        **data,
                        "success": data.get("tool_status") != "error",
                        "result": result,
                        "error": _tool_error(raw_output)
                        if data.get("tool_status") == "error"
                        else None,
                    },
                )
            data["is_update"] = True
            return StreamEvent(event_type="tool_call", data=data)

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
    return _extract_content_text(content)


def _tool_error(raw_output: Any) -> str | None:
    if not isinstance(raw_output, dict):
        return None
    for key in ("error", "message"):
        value = raw_output.get(key)
        if isinstance(value, str) and value:
            return value
    return None
