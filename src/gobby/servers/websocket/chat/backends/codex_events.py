"""Codex web-chat event normalization helpers."""

from __future__ import annotations

import json
import logging
from typing import Any

from gobby.adapters.codex_impl.app_server_adapter import CodexAdapter
from gobby.adapters.codex_impl.item_normalization import (
    build_tool_event_data,
    extract_completed_item_payload,
    looks_like_tool_item,
)
from gobby.llm.sdk_utils import parse_server_name
from gobby.sessions.transcripts.base import ParsedMessage, ParsedToolEvent
from gobby.sessions.transcripts.codex import CodexTranscriptParser

logger = logging.getLogger(__name__)


def coerce_token_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def _first_usage_count(usage: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        count = coerce_token_count(usage.get(key))
        if count is not None:
            return count
    return None


def normalize_codex_usage(usage: dict[str, Any]) -> dict[str, int | None]:
    input_tokens = _first_usage_count(usage, "input_tokens", "inputTokens")
    output_tokens = _first_usage_count(usage, "output_tokens", "outputTokens")
    cache_read_input_tokens = _first_usage_count(
        usage,
        "cache_read_input_tokens",
        "cacheReadInputTokens",
        "cached_input_tokens",
        "cachedInputTokens",
        "cache_read_tokens",
        "cacheReadTokens",
    )
    cache_creation_input_tokens = _first_usage_count(
        usage,
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
        "cache_creation_tokens",
        "cacheCreationTokens",
    )
    total_input_tokens = _first_usage_count(usage, "total_input_tokens", "totalInputTokens")

    if total_input_tokens is None and any(
        value is not None
        for value in (input_tokens, cache_read_input_tokens, cache_creation_input_tokens)
    ):
        total_input_tokens = (
            (input_tokens or 0)
            + (cache_read_input_tokens or 0)
            + (cache_creation_input_tokens or 0)
        )

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "total_input_tokens": total_input_tokens,
    }


def prefer_codex_usage(
    primary: dict[str, int | None],
    fallback: dict[str, int | None] | None,
) -> dict[str, int | None]:
    if fallback is None:
        return primary

    primary_total = primary.get("total_input_tokens")
    fallback_total = fallback.get("total_input_tokens")
    if primary_total is None or (primary_total == 0 and (fallback_total or 0) > 0):
        return fallback
    return primary


def codex_usage_from_parsed_message(record: ParsedMessage) -> dict[str, int | None] | None:
    usage = record.usage
    if usage is None:
        return None
    total_input_tokens = usage.input_tokens + usage.cache_read_tokens + usage.cache_creation_tokens
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_input_tokens": usage.cache_read_tokens,
        "cache_creation_input_tokens": usage.cache_creation_tokens,
        "total_input_tokens": total_input_tokens,
    }


def codex_context_window_from_record(record: ParsedMessage) -> int | None:
    payload = record.raw_json.get("payload")
    if not isinstance(payload, dict):
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    return coerce_token_count(info.get("model_context_window") or info.get("modelContextWindow"))


def _codex_tool_call_id(params: dict[str, Any], data: dict[str, Any]) -> str:
    for source in (data, params):
        for key in ("item_id", "id", "itemId", "call_id", "callId", "tool_use_id", "toolUseId"):
            value = source.get(key)
            if value is not None and value != "":
                return str(value)
    return "unknown"


def _codex_tool_name_from_mcp_parts(server: str | None, tool: str | None) -> str:
    if server and tool:
        return f"mcp__{server}__{tool}"
    return tool or "unknown"


def codex_record_from_notification(
    method: str,
    params: dict[str, Any],
) -> ParsedMessage | ParsedToolEvent | None:
    payload = params.get("payload")
    if isinstance(payload, dict):
        raw = dict(params)
        raw.setdefault("type", method)
    else:
        raw = {"type": method, "payload": params}
        timestamp = params.get("timestamp")
        if timestamp is not None:
            raw["timestamp"] = timestamp

    try:
        return CodexTranscriptParser().parse_line(json.dumps(raw), 0)
    except (TypeError, ValueError):
        logger.debug("Failed to parse Codex notification as transcript record", exc_info=True)
        return None


def codex_tool_event_data_from_record(
    record: ParsedMessage | ParsedToolEvent,
) -> dict[str, Any] | None:
    if isinstance(record, ParsedToolEvent):
        if not record.call_id:
            return None
        tool_name = _codex_tool_name_from_mcp_parts(record.server, record.tool)
        return {
            "phase": record.phase,
            "tool_call_id": record.call_id,
            "tool_name": tool_name,
            "server_name": record.server or parse_server_name(tool_name),
            "arguments": record.arguments,
            "success": record.error is None,
            "result": record.result,
            "error": str(record.error) if record.error is not None else None,
        }

    if record.content_type == "tool_use":
        if not record.tool_use_id:
            return None
        tool_name = record.tool_name or "unknown"
        arguments = record.tool_input or {}
        server_name = parse_server_name(tool_name)
        argument_server = arguments.get("server_name") if isinstance(arguments, dict) else None
        if isinstance(argument_server, str) and argument_server:
            server_name = argument_server
        return {
            "phase": "begin",
            "tool_call_id": record.tool_use_id,
            "tool_name": tool_name,
            "server_name": server_name,
            "arguments": arguments,
            "success": True,
            "result": None,
            "error": None,
        }

    if record.content_type == "tool_result":
        if not record.tool_use_id:
            return None
        return {
            "phase": "end",
            "tool_call_id": record.tool_use_id,
            "tool_name": "unknown",
            "server_name": "unknown",
            "arguments": {},
            "success": True,
            "result": record.tool_result,
            "error": None,
        }

    return None


def codex_tool_event_data(params: dict[str, Any]) -> dict[str, Any] | None:
    item = extract_completed_item_payload(params)
    if not item or not looks_like_tool_item(item):
        return None

    item_type = item.get("type") or item.get("itemType") or ""
    if item_type == "contextCompaction":
        return None

    data = build_tool_event_data(item, tool_name_map=CodexAdapter.TOOL_MAP)
    tool_name = data.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None

    arguments = data.get("tool_input") or {}
    if not isinstance(arguments, dict):
        arguments = {}

    raw_response = data.get("tool_response")
    result = data.get("tool_output", raw_response)
    response_is_error = bool(
        data.get("is_error")
        or (isinstance(raw_response, dict) and raw_response.get("isError"))
        or data.get("error")
    )
    error = data.get("error")
    if response_is_error and error is None:
        error = result

    return {
        "tool_call_id": _codex_tool_call_id(params, data),
        "tool_name": tool_name,
        "server_name": str(data.get("mcp_server") or parse_server_name(tool_name)),
        "arguments": arguments,
        "success": not response_is_error,
        "result": None if response_is_error else result,
        "error": str(error) if error is not None else None,
    }
