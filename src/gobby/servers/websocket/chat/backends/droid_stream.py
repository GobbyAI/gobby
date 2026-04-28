"""Droid stream-json normalization helpers."""

from __future__ import annotations

import json
import logging
from typing import Any

from gobby.adapters.gemini_acp_client import StreamEvent

logger = logging.getLogger(__name__)


def _content_delta(kind: str, **data: Any) -> StreamEvent:
    payload = {"kind": kind}
    payload.update(data)
    return StreamEvent(event_type="content_delta", data=payload)


def _token_usage_data(token_usage: Any) -> dict[str, Any]:
    if not isinstance(token_usage, dict):
        return {}
    return {
        "inputTokens": token_usage.get("inputTokens") or token_usage.get("input_tokens"),
        "outputTokens": token_usage.get("outputTokens") or token_usage.get("output_tokens"),
        "cacheReadInputTokens": token_usage.get("cacheReadTokens")
        or token_usage.get("cache_read_input_tokens"),
        "cacheCreationInputTokens": token_usage.get("cacheCreationTokens")
        or token_usage.get("cache_creation_input_tokens"),
        "thinkingTokens": token_usage.get("thinkingTokens") or token_usage.get("thinking_tokens"),
    }


def _events_from_content_blocks(content: Any) -> list[StreamEvent]:
    if not isinstance(content, list):
        return []

    events: list[StreamEvent] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                events.append(_content_delta("text", content=text))
        elif block_type == "thinking":
            thinking = block.get("thinking")
            if isinstance(thinking, str) and thinking:
                events.append(_content_delta("thinking", content=thinking))
        elif block_type == "tool_use":
            events.append(
                _content_delta(
                    "tool_use",
                    call_id=block.get("id") or "unknown",
                    tool_name=block.get("name"),
                    tool_input=block.get("input") if isinstance(block.get("input"), dict) else {},
                )
            )
        elif block_type == "tool_result":
            is_error = bool(block.get("is_error") or block.get("isError"))
            events.append(
                _content_delta(
                    "tool_result",
                    call_id=block.get("tool_use_id") or block.get("toolUseId") or "unknown",
                    success=not is_error,
                    result=block.get("content") if not is_error else None,
                    error=str(block.get("content")) if is_error else None,
                )
            )
        elif block_type == "permission_request":
            data = dict(block)
            data.pop("type", None)
            events.append(_content_delta("permission_request", **data))
    return events


def _stream_events_from_droid_jsonrpc_record(record: dict[str, Any]) -> list[StreamEvent]:
    record_kind = record.get("type")
    if record_kind == "response":
        request_id = record.get("id")
        error = record.get("error")
        if isinstance(error, dict):
            return [
                StreamEvent(
                    event_type="error",
                    data={
                        "code": error.get("code") or "upstream",
                        "message": error.get("message") or "Droid JSON-RPC error",
                        "request_id": request_id,
                        "raw_event": record,
                    },
                )
            ]

        result = record.get("result")
        if not isinstance(result, dict):
            return []
        session_id = result.get("sessionId") or result.get("session_id")
        if not session_id:
            return []
        settings = result.get("settings")
        model = settings.get("modelId") if isinstance(settings, dict) else None
        return [
            StreamEvent(
                event_type="init",
                data={
                    "session_id": session_id,
                    "model": model,
                    "request_id": request_id,
                },
            )
        ]

    if record_kind == "request":
        method = record.get("method")
        params = record.get("params")
        if method == "droid.request_permission" and isinstance(params, dict):
            events: list[StreamEvent] = []
            for item in params.get("toolUses") or []:
                if not isinstance(item, dict):
                    continue
                tool_use = item.get("toolUse")
                if not isinstance(tool_use, dict):
                    continue
                events.append(
                    _content_delta(
                        "permission_request",
                        id=tool_use.get("id") or record.get("id") or "unknown",
                        tool_name=tool_use.get("name"),
                        tool_input=(
                            tool_use.get("input") if isinstance(tool_use.get("input"), dict) else {}
                        ),
                        request_id=record.get("id"),
                        options=params.get("options"),
                        confirmation_type=item.get("confirmationType"),
                    )
                )
            return events
        return []

    if record_kind != "notification" or record.get("method") != "droid.session_notification":
        return []

    params = record.get("params")
    notification = params.get("notification") if isinstance(params, dict) else None
    if not isinstance(notification, dict):
        return []

    notification_type = notification.get("type")
    if notification_type == "assistant_text_delta":
        text = notification.get("textDelta")
        return [_content_delta("text", content=text)] if isinstance(text, str) and text else []
    if notification_type == "thinking_text_delta":
        text = notification.get("textDelta")
        return [_content_delta("thinking", content=text)] if isinstance(text, str) and text else []
    if notification_type == "tool_call":
        tool_use = notification.get("toolUse")
        if not isinstance(tool_use, dict):
            return []
        return [
            _content_delta(
                "tool_use",
                call_id=tool_use.get("id") or "unknown",
                tool_name=tool_use.get("name"),
                tool_input=tool_use.get("input") if isinstance(tool_use.get("input"), dict) else {},
            )
        ]
    if notification_type == "tool_result":
        is_error = bool(notification.get("isError") or notification.get("is_error"))
        content = notification.get("content")
        return [
            _content_delta(
                "tool_result",
                call_id=notification.get("toolUseId")
                or notification.get("tool_use_id")
                or "unknown",
                success=not is_error,
                result=content if not is_error else None,
                error=str(content) if is_error else None,
            )
        ]
    if notification_type == "error":
        return [
            StreamEvent(
                event_type="error",
                data={
                    "code": notification.get("errorType") or "upstream",
                    "message": notification.get("message") or "Droid error",
                    "raw_event": record,
                },
            )
        ]
    if (
        notification_type == "droid_working_state_changed"
        and notification.get("newState") == "idle"
    ):
        return [StreamEvent(event_type="result", data=record)]
    if notification_type == "session_token_usage_changed":
        return [
            StreamEvent(
                event_type="usage",
                data={
                    "session_id": notification.get("sessionId"),
                    "usage": _token_usage_data(notification.get("tokenUsage")),
                },
            )
        ]

    return []


def _stream_events_from_droid_record(record: dict[str, Any]) -> list[StreamEvent]:
    if record.get("jsonrpc") == "2.0":
        return _stream_events_from_droid_jsonrpc_record(record)

    record_type = record.get("type")
    if record_type in {"session_start", "init"} or (
        record_type == "system" and record.get("subtype") == "init"
    ):
        return [
            StreamEvent(
                event_type="init",
                data={
                    "session_id": record.get("session_id") or record.get("sessionId"),
                    "model": record.get("model"),
                },
            )
        ]
    if record_type in {"result", "done", "completion"}:
        return [StreamEvent(event_type="result", data=record)]
    if record_type == "error":
        return [
            StreamEvent(
                event_type="error",
                data={
                    "code": record.get("code") or "upstream",
                    "message": record.get("message") or record.get("error") or "Droid error",
                },
            )
        ]
    if record_type == "content_delta":
        kind = str(record.get("kind") or "text")
        data = dict(record)
        data.pop("type", None)
        data["kind"] = kind
        return [StreamEvent(event_type="content_delta", data=data)]
    if record_type == "permission_request":
        data = dict(record)
        data.pop("type", None)
        return [_content_delta("permission_request", **data)]
    if record_type == "reasoning":
        text = record.get("text")
        return [_content_delta("thinking", content=text)] if isinstance(text, str) and text else []
    if record_type == "tool_call":
        return [
            _content_delta(
                "tool_use",
                call_id=record.get("id") or "unknown",
                tool_name=record.get("toolName") or record.get("tool_name"),
                tool_input=record.get("parameters")
                if isinstance(record.get("parameters"), dict)
                else {},
            )
        ]
    if record_type == "tool_result":
        is_error = bool(record.get("isError") or record.get("is_error"))
        return [
            _content_delta(
                "tool_result",
                call_id=record.get("id") or "unknown",
                success=not is_error,
                result=record.get("value") if not is_error else None,
                error=str(record.get("value")) if is_error else None,
            )
        ]
    if record_type != "message":
        return [
            StreamEvent(
                event_type="error",
                data={
                    "code": "unhandled",
                    "message": f"Unhandled Droid stream event type: {record_type!r}",
                    "raw_event": record,
                },
            )
        ]

    message = record.get("message")
    if isinstance(message, dict):
        if message.get("role") not in {None, "assistant"}:
            return []
        return _events_from_content_blocks(message.get("content") or [])

    if record.get("role") != "assistant":
        return []
    text = record.get("text")
    return [_content_delta("text", content=text)] if isinstance(text, str) and text else []


def _parse_droid_stream_line(line: bytes | str) -> list[StreamEvent]:
    text = line.decode("utf-8", errors="replace") if isinstance(line, bytes) else line
    text = text.strip()
    if not text:
        return []
    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Skipping malformed Droid stream-json line: %s", exc)
        return []
    if not isinstance(record, dict):
        logger.warning("Skipping non-object Droid stream-json line")
        return []
    return _stream_events_from_droid_record(record)


__all__ = ["_parse_droid_stream_line"]
