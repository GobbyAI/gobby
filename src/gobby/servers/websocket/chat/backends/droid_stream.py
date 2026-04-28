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


def _stream_events_from_droid_record(record: dict[str, Any]) -> list[StreamEvent]:
    record_type = record.get("type")
    if record_type in {"session_start", "init"}:
        return [
            StreamEvent(
                event_type="init",
                data={
                    "session_id": record.get("session_id") or record.get("sessionId"),
                    "model": record.get("model"),
                },
            )
        ]
    if record_type in {"result", "done"}:
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
    if not isinstance(message, dict) or message.get("role") not in {None, "assistant"}:
        return []
    content = message.get("content") or []
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
            is_error = bool(block.get("is_error"))
            events.append(
                _content_delta(
                    "tool_result",
                    call_id=block.get("tool_use_id") or "unknown",
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
