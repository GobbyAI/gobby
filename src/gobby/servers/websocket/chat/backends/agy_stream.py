"""AGY stream-json normalization helpers."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

from gobby.adapters.acp_stream import StreamEvent
from gobby.adapters.agy_contract import AGY_TOOL_MAP
from gobby.hooks.normalization import normalize_tool_fields
from gobby.workflows.enforcement.blocking import canonical_gobby_tool_name

logger = logging.getLogger(__name__)

_BOOKKEEPING_STEP_TYPES = frozenset(
    {
        "user_input",
        "checkpoint",
        "system_message",
        "error_message",
        "unknown",
    }
)


def _content_delta(kind: str, **data: Any) -> StreamEvent:
    payload = {"kind": kind}
    payload.update(data)
    return StreamEvent(event_type="content_delta", data=payload)


def agy_tool_name_adapter(
    raw_tool_name: str,
    tool_input: Mapping[str, Any] | None = None,
) -> str:
    """Map AGY snake_case (and MCP) spellings to Gobby canonical tool names."""
    mapped = raw_tool_name
    if raw_tool_name == "call_mcp_tool" and isinstance(tool_input, Mapping):
        server = tool_input.get("ServerName")
        tool = tool_input.get("ToolName")
        if isinstance(server, str) and server and isinstance(tool, str) and tool:
            mapped = f"mcp__{server}__{tool}"
        else:
            mapped = AGY_TOOL_MAP.get(raw_tool_name, raw_tool_name)
    else:
        mapped = AGY_TOOL_MAP.get(raw_tool_name, raw_tool_name)
    normalized = normalize_tool_fields({"tool_name": mapped})
    name = normalized.get("tool_name")
    if not isinstance(name, str) or not name:
        name = mapped
    return canonical_gobby_tool_name(name)


def _nested_body(record: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    event_name = record.get("event")
    if not isinstance(event_name, str) or not event_name:
        return None
    body = record.get(event_name)
    if not isinstance(body, dict):
        return None
    return event_name, body


def _tool_error_text(tool_info: Mapping[str, Any]) -> str | None:
    error = tool_info.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        return message if isinstance(message, str) else None
    if error is None:
        return None
    return str(error)


def _tool_call_id(body: Mapping[str, Any]) -> str:
    conversation_id = body.get("conversation_id") or "unknown"
    step_index = body.get("step_index")
    return f"{conversation_id}:{step_index}"


def _events_from_tool_step(body: dict[str, Any]) -> list[StreamEvent]:
    state = body.get("state")
    tool_info = body.get("tool_info")
    info: dict[str, Any] = tool_info if isinstance(tool_info, dict) else {}
    parameters = info.get("parameters")
    tool_input = parameters if isinstance(parameters, dict) else {}
    raw_name = body.get("tool_name") or info.get("name") or "unknown"
    tool_name = agy_tool_name_adapter(str(raw_name), tool_input)
    call_id = _tool_call_id(body)
    if state == "ACTIVE":
        return [
            _content_delta(
                "tool_use",
                call_id=call_id,
                tool_name=tool_name,
                tool_input=tool_input,
            )
        ]
    if state == "DONE":
        return [
            _content_delta(
                "tool_result",
                call_id=call_id,
                success=True,
                result=info.get("output"),
                error=None,
            )
        ]
    if state == "ERROR":
        return [
            _content_delta(
                "tool_result",
                call_id=call_id,
                success=False,
                result=None,
                error=_tool_error_text(info),
            )
        ]
    return []


def _events_from_step_update(body: dict[str, Any]) -> list[StreamEvent]:
    step_type = body.get("step_type")
    if step_type in _BOOKKEEPING_STEP_TYPES or not isinstance(step_type, str):
        return []
    if step_type == "agent_response":
        text = body.get("text_delta")
        if isinstance(text, str) and text:
            return [_content_delta("text", content=text)]
        return []
    if step_type == "tool":
        return _events_from_tool_step(body)
    return []


def _events_from_init(record: dict[str, Any], body: dict[str, Any]) -> list[StreamEvent]:
    conversation_id = record.get("conversation_id") or body.get("conversation_id")
    return [
        StreamEvent(
            event_type="init",
            data={
                "session_id": conversation_id,
                "conversation_id": conversation_id,
                "model": body.get("model"),
                "cwd": body.get("cwd"),
                "tools": body.get("tools"),
                "permission_mode": body.get("permission_mode"),
            },
        )
    ]


def _events_from_result(body: dict[str, Any]) -> list[StreamEvent]:
    data: dict[str, Any] = {
        "conversation_id": body.get("conversation_id"),
        "status": body.get("status"),
        "num_turns": body.get("num_turns"),
        "duration_seconds": body.get("duration_seconds"),
    }
    if "usage" in body:
        data["usage"] = body["usage"]
    if "error" in body:
        data["error"] = body["error"]
    return [StreamEvent(event_type="result", data=data)]


def _stream_events_from_agy_record(record: dict[str, Any]) -> list[StreamEvent]:
    nested = _nested_body(record)
    if nested is None:
        return []
    event_name, body = nested
    if event_name == "init":
        return _events_from_init(record, body)
    if event_name == "step_update":
        return _events_from_step_update(body)
    if event_name == "result":
        return _events_from_result(body)
    return []


def parse_agy_stream_line(line: bytes | str) -> list[StreamEvent]:
    if isinstance(line, bytes):
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError as exc:
            logger.warning("Skipping malformed AGY stream-json line: %s", exc)
            return []
    else:
        text = line
    text = text.strip()
    if not text:
        return []
    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Skipping malformed AGY stream-json line: %s", exc)
        return []
    if not isinstance(record, dict):
        logger.warning("Skipping non-object AGY stream-json line")
        return []
    try:
        return _stream_events_from_agy_record(record)
    except (TypeError, KeyError, AttributeError, ValueError) as exc:
        logger.warning("Skipping malformed AGY stream-json record: %s", exc)
        return []


async def iter_agy_turn(lines: AsyncIterator[bytes | str]) -> AsyncIterator[StreamEvent]:
    """Yield one turn of AGY stream events, stopping after the ``result`` record."""
    seen_init = False
    emitted = False
    async for line in lines:
        for event in parse_agy_stream_line(line):
            if event.event_type == "init":
                if seen_init or emitted:
                    continue
                seen_init = True
            emitted = True
            yield event
            if event.event_type == "result":
                return
    yield StreamEvent(event_type="error", data={"code": "eof"})


__all__ = ["agy_tool_name_adapter", "iter_agy_turn", "parse_agy_stream_line"]
