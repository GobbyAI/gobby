"""Transcript parser for AGY `transcript_full.jsonl` session files."""

from __future__ import annotations

import json
import logging
from collections import deque
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.adapters.agy_contract import (
    decode_agy_tool_args,
    normalize_agy_tool_name,
    parse_agy_command_exit,
)
from gobby.sessions.transcripts.base import (
    UNMODELED_RECORD_CONTENT_TYPE,
    BaseTranscriptParser,
    ParsedMessage,
    ParsedToolEvent,
    ParseEvent,
    RawLine,
    annotate_record_source,
)

_SYSTEM_SOURCES = frozenset({"SYSTEM", "SYSTEM_SDK"})
_TOKEN_EFFICIENT_BASENAME = "transcript.jsonl"


def _parse_timestamp(raw: Any) -> datetime:
    if not isinstance(raw, str) or not raw:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _basename(path: Path | str | None) -> str:
    if path is None:
        return ""
    return Path(path).name


def _label(value: Any) -> str:
    return value if isinstance(value, str) and value else "<missing>"


@dataclass
class _PendingCall:
    call_id: str
    tool: str
    arguments: dict[str, Any]


class AgyTranscriptParser(BaseTranscriptParser):
    """Parse AGY JSONL records into messages and positional tool events."""

    supports_incremental_state = True

    def __init__(
        self,
        session_id: str | None = None,
        logger_instance: logging.Logger | None = None,
        transcript_path: Path | str | None = None,
    ) -> None:
        super().__init__(
            cli_name="agy",
            session_id=session_id,
            logger_instance=logger_instance,
            transcript_path=transcript_path,
        )
        self._pending: deque[_PendingCall] = deque()
        self._decode_jsonl_args = _basename(transcript_path) == _TOKEN_EFFICIENT_BASENAME

    def snapshot_state(self) -> dict[str, Any]:
        return {
            "pending_calls": [
                {
                    "call_id": pending.call_id,
                    "tool": pending.tool,
                    "arguments": dict(pending.arguments),
                }
                for pending in self._pending
            ]
        }

    def hydrate_state(self, state: Mapping[str, Any]) -> None:
        raw_queue = state.get("pending_calls")
        pending: deque[_PendingCall] = deque()
        if isinstance(raw_queue, list):
            for item in raw_queue:
                if not isinstance(item, dict):
                    continue
                call_id = item.get("call_id")
                tool = item.get("tool")
                arguments = item.get("arguments")
                if not isinstance(call_id, str) or not isinstance(tool, str):
                    continue
                pending.append(
                    _PendingCall(
                        call_id=call_id,
                        tool=tool,
                        arguments=arguments if isinstance(arguments, dict) else {},
                    )
                )
        self._pending = pending

    def parse_line(self, line: str, index: int) -> ParsedMessage | ParsedToolEvent | None:
        expanded = self._expand_line(line, index)
        return expanded[0] if expanded else None

    def iter_parse_events(
        self, raw_lines: Iterable[RawLine], start_index: int = 0
    ) -> Iterator[ParseEvent]:
        current_index = start_index
        for raw in raw_lines:
            if not raw.text.strip():
                continue
            start_idx = current_index
            record = self._decode_line(raw.text, current_index)
            expanded = [] if record is None else self._expand_record(record, current_index)
            for item in expanded:
                if isinstance(item, ParsedMessage):
                    item.index = current_index
                current_index += 1
            if record is None:
                # An undecodable line still occupies one position and yields an
                # event, as the base parser does, so byte offsets and parsed
                # indices stay monotonic across the emitted stream.
                current_index += 1
            elif not expanded:
                continue
            yield ParseEvent(
                byte_offset=raw.byte_offset,
                raw_line_no=raw.raw_line_no,
                parsed_index=start_idx,
                records=annotate_record_source(
                    expanded,
                    source=self.cli_name,
                    raw_line_no=raw.raw_line_no,
                ),
                parser_safe=True,
            )

    def extract_last_messages(
        self,
        turns: list[dict[str, Any]],
        num_pairs: int = 2,
        *,
        include_tool_activity: bool = False,
    ) -> list[dict[str, Any]]:
        del include_tool_activity
        messages: list[dict[str, Any]] = []
        for turn in reversed(turns):
            if not isinstance(turn, dict):
                continue
            source = turn.get("source")
            record_type = turn.get("type")
            content = turn.get("content")
            if (
                source == "USER_EXPLICIT"
                and record_type == "USER_INPUT"
                and isinstance(content, str)
                and content
            ):
                messages.append({"role": "user", "content": content})
            elif (
                source == "MODEL"
                and record_type == "PLANNER_RESPONSE"
                and isinstance(content, str)
                and content
            ):
                messages.append({"role": "assistant", "content": content})
            if len(messages) >= num_pairs * 2:
                break
        return list(reversed(messages))

    def extract_turns_since_clear(
        self, turns: list[dict[str, Any]], max_turns: int | None = None
    ) -> list[dict[str, Any]]:
        if max_turns is None:
            return list(turns)
        return list(turns)[-max_turns:]

    def is_session_boundary(self, turn: dict[str, Any]) -> bool:
        del turn
        return False

    def _decode_line(self, line: str, index: int) -> dict[str, Any] | None:
        """Return the JSON object on ``line``, or ``None`` for an undecodable line."""
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            self.error_log.log_decode_failure(index, self.session_id, line, exc)
            return None
        if not isinstance(record, dict):
            self.error_log.log_decode_failure(index, self.session_id, line, None)
            return None
        return record

    def _expand_line(self, line: str, index: int) -> list[ParsedMessage | ParsedToolEvent]:
        record = self._decode_line(line, index)
        return [] if record is None else self._expand_record(record, index)

    def _expand_record(
        self, record: dict[str, Any], index: int
    ) -> list[ParsedMessage | ParsedToolEvent]:
        source = record.get("source")
        record_type = record.get("type")
        timestamp = _parse_timestamp(record.get("created_at"))
        if not isinstance(source, str) or not isinstance(record_type, str):
            return [self._unmodeled(index, source, record_type, record, timestamp)]
        if source in _SYSTEM_SOURCES:
            return []
        if source == "USER_EXPLICIT":
            if record_type != "USER_INPUT":
                return [self._unmodeled(index, source, record_type, record, timestamp)]
            content = record.get("content")
            if not isinstance(content, str) or not content:
                return []
            return [self._message(index, "user", content, "text", timestamp, record)]
        if source != "MODEL":
            return [self._unmodeled(index, source, record_type, record, timestamp)]
        if record_type == "PLANNER_RESPONSE":
            return self._expand_planner(record, index, timestamp)
        return self._expand_tool_result(record, index, timestamp, record_type)

    def _unmodeled(
        self,
        index: int,
        source: Any,
        record_type: Any,
        record: dict[str, Any],
        timestamp: datetime,
    ) -> ParsedMessage:
        """Build the non-rendering sentinel for an unknown ``source``/``type`` pair.

        Mirrors the Claude parser: the record is logged, keeps its position in
        the stream, and reaches the unmodeled-observation worklist instead of
        vanishing silently.
        """
        label = f"{_label(source)}/{_label(record_type)}"
        self.error_log.log_unknown_block(index, self.session_id, label, record)
        return self._message(
            index, "system", label, UNMODELED_RECORD_CONTENT_TYPE, timestamp, record
        )

    def _expand_planner(
        self,
        record: dict[str, Any],
        index: int,
        timestamp: datetime,
    ) -> list[ParsedMessage | ParsedToolEvent]:
        out: list[ParsedMessage | ParsedToolEvent] = []
        thinking = record.get("thinking")
        if isinstance(thinking, str) and thinking:
            out.append(self._message(index, "assistant", thinking, "thinking", timestamp, record))
        content = record.get("content")
        if isinstance(content, str) and content:
            out.append(self._message(index, "assistant", content, "text", timestamp, record))
        tool_calls = record.get("tool_calls")
        if not isinstance(tool_calls, list):
            return out
        step_index = record.get("step_index")
        step = step_index if isinstance(step_index, int) and not isinstance(step_index, bool) else 0
        for ordinal, raw_call in enumerate(tool_calls):
            if not isinstance(raw_call, dict):
                continue
            name = raw_call.get("name")
            tool = normalize_agy_tool_name(name if isinstance(name, str) else "tool")
            arguments = self._tool_arguments(raw_call.get("args"))
            call_id = self._call_id(step, ordinal)
            self._pending.append(_PendingCall(call_id=call_id, tool=tool, arguments=arguments))
            out.append(
                ParsedToolEvent(
                    phase="begin",
                    call_id=call_id,
                    server=None,
                    tool=tool,
                    arguments=arguments,
                    timestamp=timestamp,
                    raw_json=record,
                )
            )
        return out

    def _expand_tool_result(
        self,
        record: dict[str, Any],
        index: int,
        timestamp: datetime,
        record_type: str,
    ) -> list[ParsedMessage | ParsedToolEvent]:
        del index
        if not self._pending:
            return []
        pending = self._pending.popleft()
        content = record.get("content")
        content_text = content if isinstance(content, str) else ""
        status = record.get("status")
        status_text = status if isinstance(status, str) else "DONE"
        result: dict[str, Any] = {
            "content": content_text,
            "agy_status": status_text,
            "result_type": record_type,
        }
        exit_code = parse_agy_command_exit(content_text)
        if status_text == "RUNNING":
            result["status"] = "RUNNING"
            result["unknown_reason"] = "nonterminal"
        elif exit_code == 0 and status_text == "ERROR":
            result["unknown_reason"] = "conflicting_outcomes"
        elif exit_code is not None:
            result["exit_code"] = exit_code
        elif pending.tool == "Bash":
            result["unknown_reason"] = "unstructured"
        return [
            ParsedToolEvent(
                phase="end",
                call_id=pending.call_id,
                server=None,
                tool=pending.tool,
                arguments=pending.arguments,
                timestamp=timestamp,
                raw_json=record,
                result=result,
            )
        ]

    def _tool_arguments(self, raw: Any) -> dict[str, Any]:
        decoded = raw
        if self._decode_jsonl_args:
            if isinstance(raw, dict):
                decoded = {key: decode_agy_tool_args(value) for key, value in raw.items()}
            else:
                decoded = decode_agy_tool_args(raw)
        if not isinstance(decoded, dict):
            return {} if decoded is None else {"input": decoded}
        arguments = dict(decoded)
        command_line = arguments.get("CommandLine")
        if "command" not in arguments and isinstance(command_line, str):
            arguments["command"] = command_line
        return arguments

    def _call_id(self, step_index: int, ordinal: int) -> str:
        conversation = self.session_id or "agy"
        return f"{conversation}:{step_index}:{ordinal}"

    def _message(
        self,
        index: int,
        role: str,
        content: str,
        content_type: str,
        timestamp: datetime,
        raw: dict[str, Any],
    ) -> ParsedMessage:
        return ParsedMessage(
            index=index,
            role=role,
            content=content,
            content_type=content_type,
            tool_name=None,
            tool_input=None,
            tool_result=None,
            timestamp=timestamp,
            raw_json=raw,
            usage=None,
        )
