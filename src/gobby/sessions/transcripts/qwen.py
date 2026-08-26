"""Qwen transcript parser for the CLI's current JSONL envelope."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from gobby.sessions.token_usage import typed_json_token_usage
from gobby.sessions.transcripts.base import (
    BaseTranscriptParser,
    ParsedMessage,
    ParseEvent,
    RawLine,
    TokenUsage,
    _unknown_block_message,
    annotate_record_source,
)
from gobby.sessions.transcripts.tool_activity import event_activity_by_user_index

_IGNORED_SYSTEM_SUBTYPES = {"file_history_snapshot", "ui_telemetry"}


def _result_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


class QwenTranscriptParser(BaseTranscriptParser):
    """Parse current ``{type, message: {role, parts}}`` Qwen JSONL records."""

    def __init__(
        self,
        session_id: str | None = None,
        logger_instance: logging.Logger | None = None,
    ) -> None:
        super().__init__(cli_name="qwen", session_id=session_id, logger_instance=logger_instance)
        self._last_tool_use_id: str | None = None

    def _extract_usage(self, data: dict[str, Any]) -> TokenUsage | None:
        usage_data = data.get("usageMetadata") or data.get("tokens")
        if isinstance(usage_data, dict):
            return typed_json_token_usage(usage_data)
        return None

    def _stable_message_id(
        self,
        record: dict[str, Any],
        index: int,
        part_index: int | None = None,
    ) -> str:
        raw_id = record.get("uuid") or record.get("id")
        if isinstance(raw_id, str) and raw_id.strip():
            normalized = raw_id.strip()
            return f"{normalized}:{part_index}" if part_index is not None else normalized
        session_prefix = self.session_id or self.cli_name
        return f"{session_prefix}:jsonl:{index}"

    def _next_tool_use_id(
        self,
        call_id: str | None,
        *,
        index: int,
        tool_name: str | None,
    ) -> str:
        if call_id:
            return call_id
        payload = "|".join((self.cli_name, self.session_id or "", str(index), tool_name or ""))
        digest = sha256(payload.encode()).hexdigest()[:16]
        return f"qwen-tu-{digest}"

    def _unknown_record(
        self,
        *,
        record: dict[str, Any],
        block_type: str,
        index: int,
        role: str = "assistant",
        model: str | None = None,
        usage: TokenUsage | None = None,
    ) -> ParsedMessage:
        return _unknown_block_message(
            index=index,
            block_type=block_type,
            raw=record,
            role=role,
            timestamp=_parse_timestamp(record.get("timestamp")),
            message_id=self._stable_message_id(record, index),
            model=model,
            usage=usage,
        )

    def _expand_line(self, line: str, index: int) -> list[ParsedMessage]:
        """Expand one Qwen envelope into sequential normalized messages."""
        if not line.strip():
            return []
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            self.error_log.log_decode_failure(index, self.session_id, line, exc)
            return []
        if not isinstance(record, dict):
            self.error_log.log_decode_failure(index, self.session_id, line, None)
            return []

        timestamp = _parse_timestamp(record.get("timestamp"))
        raw_model = record.get("model")
        model = raw_model if isinstance(raw_model, str) else None
        raw_record_type = record.get("type")
        record_type = raw_record_type if isinstance(raw_record_type, str) else "<missing>"

        if record_type == "system":
            raw_subtype = record.get("subtype")
            subtype = raw_subtype if isinstance(raw_subtype, str) else "system"
            if subtype in _IGNORED_SYSTEM_SUBTYPES:
                return []
            return [
                self._unknown_record(
                    record=record,
                    block_type=subtype,
                    index=index,
                    role="system",
                    model=model,
                )
            ]

        if record_type not in {"assistant", "tool_result", "user"}:
            return [
                self._unknown_record(
                    record=record,
                    block_type=record_type,
                    index=index,
                    model=model,
                )
            ]

        message = record.get("message")
        if not isinstance(message, dict):
            return [
                self._unknown_record(
                    record=record,
                    block_type=record_type,
                    index=index,
                    model=model,
                    usage=self._extract_usage(record),
                )
            ]
        parts = message.get("parts")
        if not isinstance(parts, list):
            return [
                self._unknown_record(
                    record=record,
                    block_type=record_type,
                    index=index,
                    model=model,
                    usage=self._extract_usage(record),
                )
            ]

        role = {"assistant": "assistant", "tool_result": "tool", "user": "user"}[record_type]
        usage = self._extract_usage(record) if role == "assistant" else None
        out: list[ParsedMessage] = []

        def consume_usage() -> TokenUsage | None:
            nonlocal usage
            current = usage
            usage = None
            return current

        def append_message(
            *,
            part_index: int,
            content: str,
            content_type: str,
            message_role: str = role,
            tool_name: str | None = None,
            tool_input: dict[str, Any] | None = None,
            tool_result: dict[str, Any] | None = None,
            tool_use_id: str | None = None,
        ) -> None:
            message_index = index + len(out)
            out.append(
                ParsedMessage(
                    index=message_index,
                    role=message_role,
                    content=content,
                    content_type=content_type,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tool_result=tool_result,
                    timestamp=timestamp,
                    raw_json=record,
                    usage=consume_usage() if message_role == "assistant" else None,
                    model=model,
                    tool_use_id=tool_use_id,
                    message_id=self._stable_message_id(record, message_index, part_index),
                )
            )

        for part_index, part in enumerate(parts):
            if not isinstance(part, dict):
                raw_part = {"value": part}
                message_index = index + len(out)
                out.append(
                    _unknown_block_message(
                        index=message_index,
                        block_type="<invalid_part>",
                        raw=raw_part,
                        role=role,
                        timestamp=timestamp,
                        message_id=self._stable_message_id(record, message_index, part_index),
                        model=model,
                        usage=consume_usage() if role == "assistant" else None,
                    )
                )
                continue

            function_call = part.get("functionCall")
            if isinstance(function_call, dict):
                raw_tool_name = function_call.get("name")
                tool_name = raw_tool_name if isinstance(raw_tool_name, str) else None
                raw_tool_input = function_call.get("args")
                tool_input = raw_tool_input if isinstance(raw_tool_input, dict) else {}
                raw_call_id = function_call.get("id")
                call_id = raw_call_id if isinstance(raw_call_id, str) else None
                tool_use_id = self._next_tool_use_id(
                    call_id,
                    index=index + len(out),
                    tool_name=tool_name,
                )
                self._last_tool_use_id = tool_use_id
                append_message(
                    part_index=part_index,
                    content=f"Tool call: {tool_name or 'unknown'}",
                    content_type="tool_use",
                    tool_name=tool_name,
                    tool_input=tool_input,
                    tool_use_id=tool_use_id,
                )
                continue
            if "functionCall" in part:
                block_type = "functionCall"
            else:
                function_response = part.get("functionResponse")
                if isinstance(function_response, dict):
                    raw_tool_name = function_response.get("name")
                    tool_name = raw_tool_name if isinstance(raw_tool_name, str) else None
                    raw_tool_result = record.get("toolCallResult")
                    result_metadata = raw_tool_result if isinstance(raw_tool_result, dict) else {}
                    raw_call_id = function_response.get("id") or result_metadata.get("callId")
                    call_id = (
                        raw_call_id if isinstance(raw_call_id, str) else self._last_tool_use_id
                    )
                    response = function_response.get("response")
                    output = (
                        response.get("output")
                        if isinstance(response, dict) and "output" in response
                        else response
                    )
                    append_message(
                        part_index=part_index,
                        content=_result_content(output),
                        content_type="tool_result",
                        message_role="tool",
                        tool_name=tool_name,
                        tool_result={
                            "output": output,
                            "status": result_metadata.get("status", "unknown"),
                        },
                        tool_use_id=call_id,
                    )
                    continue
                if "functionResponse" in part:
                    block_type = "functionResponse"
                else:
                    raw_text = part.get("text")
                    if isinstance(raw_text, str):
                        if not raw_text:
                            continue
                        append_message(
                            part_index=part_index,
                            content=raw_text,
                            content_type="thinking" if part.get("thought") is True else "text",
                        )
                        continue
                    raw_part_type = part.get("type")
                    if isinstance(raw_part_type, str):
                        block_type = raw_part_type
                    else:
                        block_type = next(iter(part), "<missing>")

            message_index = index + len(out)
            out.append(
                _unknown_block_message(
                    index=message_index,
                    block_type=block_type,
                    raw=part,
                    role=role,
                    timestamp=timestamp,
                    message_id=self._stable_message_id(record, message_index, part_index),
                    model=model,
                    usage=consume_usage() if role == "assistant" else None,
                )
            )

        return out

    def parse_line(self, line: str, index: int) -> ParsedMessage | None:
        """Return the first expanded part for single-record callers."""
        expanded = self._expand_line(line, index)
        return expanded[0] if expanded else None

    def iter_parse_events(
        self, raw_lines: Iterable[RawLine], start_index: int = 0
    ) -> Iterator[ParseEvent]:
        """Stream Qwen envelopes with one sequential index per expanded part."""
        current_index = start_index
        for raw in raw_lines:
            if not raw.text.strip():
                continue
            expanded = self._expand_line(raw.text, current_index)
            if not expanded:
                continue
            start_idx = current_index
            for message in expanded:
                message.index = current_index
                current_index += 1
            records = annotate_record_source(
                expanded,
                source=self.cli_name,
                raw_line_no=raw.raw_line_no,
            )
            yield ParseEvent(
                byte_offset=raw.byte_offset,
                raw_line_no=raw.raw_line_no,
                parsed_index=start_idx,
                records=records,
                parser_safe=True,
            )

    def extract_last_messages(
        self,
        turns: list[dict[str, Any]],
        num_pairs: int = 2,
        *,
        include_tool_activity: bool = False,
    ) -> list[dict[str, Any]]:
        """Extract visible text and tool-call labels from current Qwen envelopes."""
        messages: list[dict[str, Any]] = []
        activity = event_activity_by_user_index(self, turns) if include_tool_activity else {}
        for turn_index in range(len(turns) - 1, -1, -1):
            turn = turns[turn_index]
            record_type = turn.get("type")
            if record_type not in {"assistant", "user"}:
                continue
            message = turn.get("message")
            if not isinstance(message, dict):
                continue
            parts = message.get("parts")
            if not isinstance(parts, list):
                continue
            content_parts: list[str] = []
            for part in parts:
                if not isinstance(part, dict):
                    continue
                raw_text = part.get("text")
                if isinstance(raw_text, str) and (
                    record_type == "user" or part.get("thought") is not True
                ):
                    content_parts.append(raw_text)
                function_call = part.get("functionCall")
                if record_type == "assistant" and isinstance(function_call, dict):
                    tool_name = function_call.get("name") or "unknown"
                    content_parts.append(f"[Tool call: {tool_name}]")
            content = "\n".join(part for part in content_parts if part)
            if not content:
                continue
            role = "assistant" if record_type == "assistant" else "user"
            extracted: dict[str, Any] = {"role": role, "content": content}
            if role == "user" and turn_index in activity:
                extracted["tool_activity"] = activity[turn_index]
            messages.insert(0, extracted)
            if len(messages) >= num_pairs * 2:
                break
        return messages

    def extract_turns_since_clear(
        self, turns: list[dict[str, Any]], max_turns: int | None = None
    ) -> list[dict[str, Any]]:
        return turns[-max_turns:] if max_turns and len(turns) > max_turns else turns

    def is_session_boundary(self, turn: dict[str, Any]) -> bool:
        del turn
        return False
