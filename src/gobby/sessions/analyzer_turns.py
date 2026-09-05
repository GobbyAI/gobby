"""Provider-neutral transcript turns for :mod:`gobby.sessions.analyzer`."""

from __future__ import annotations

import json
from bisect import bisect_right
from collections import deque
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from gobby.sessions.transcripts.base import ParsedMessage, raw_lines_from_texts
from gobby.sessions.transcripts.tool_activity import (
    ToolActivityEntry,
    canonical_tool_name,
    codex_item_activity,
    escape_ledger_text,
    fresh_scan_parser,
    is_commit_producing,
    pending_exec_command,
)

_RESULT_TEXT_MAX_CHARS = 500
SUMMARY_ANALYZER_MAX_RECORDS = 20_000


@dataclass
class _AdaptedCall:
    record_index: int
    name: str
    tool_input: dict[str, Any]
    tool_use_id: str
    use_block: dict[str, Any]
    result_block: dict[str, Any] | None = None
    result_record_index: int | None = None


type _ActivityKey = tuple[int | None, str, Hashable]
type _RecordBlocks = dict[int, dict[int, dict[str, Any]]]
type _JSONInput = str | int | float | bool | None | list[_JSONInput] | dict[str, _JSONInput]


def analyzer_turns_from_transcript(
    parser: Any, turns: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Run an observational parser scan and materialize Claude-shaped turns."""
    scan = fresh_scan_parser(parser)
    blocks_by_record: _RecordBlocks = {}
    roles_by_record: dict[int, str] = {}
    calls: dict[int, _AdaptedCall] = {}
    calls_by_id: dict[str, _AdaptedCall] = {}
    user_record_indexes: list[int] = []
    texts = (json.dumps(turn, default=str) for turn in turns)

    for event in scan.iter_parse_events(raw_lines_from_texts(texts)):
        record_index = event.raw_line_no
        for record in event.records:
            if not isinstance(record, ParsedMessage):
                continue
            if record.content_type == "text" and record.role in {"user", "assistant"}:
                if record.role == "user" and (
                    not user_record_indexes or user_record_indexes[-1] != record_index
                ):
                    user_record_indexes.append(record_index)
                _append_block(
                    blocks_by_record,
                    roles_by_record,
                    record_index,
                    record.role,
                    {"type": "text", "text": _content_text(record.content)},
                )
            elif record.content_type == "tool_use":
                raw_input = (
                    dict(record.tool_input) if isinstance(record.tool_input, Mapping) else {}
                )
                command = pending_exec_command(record.tool_name or "", raw_input)
                name, tool_input = canonical_tool_name(record.tool_name, raw_input)
                if command is not None:
                    name, tool_input = "Bash", {"command": command}
                tool_use_id = record.tool_use_id or f"tool-{record_index}-{len(calls)}"
                tool_use_block: dict[str, Any] = {
                    "type": "tool_use",
                    "name": name,
                    "input": tool_input,
                    "id": tool_use_id,
                }
                _append_block(
                    blocks_by_record,
                    roles_by_record,
                    record_index,
                    "assistant",
                    tool_use_block,
                )
                call = _AdaptedCall(
                    record_index=record_index,
                    name=name,
                    tool_input=tool_input,
                    tool_use_id=tool_use_id,
                    use_block=tool_use_block,
                )
                calls[id(call)] = call
                calls_by_id[tool_use_id] = call
            elif record.content_type == "tool_result":
                tool_use_id = record.tool_use_id or ""
                result = record.tool_result if isinstance(record.tool_result, Mapping) else {}
                error = _result_error(result)
                matched_call = calls_by_id.get(tool_use_id)
                output = _result_content(record.content, result)
                result_block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": _retained_result(matched_call, output, error),
                    "is_error": error is not None,
                }
                _append_block(
                    blocks_by_record,
                    roles_by_record,
                    record_index,
                    "user",
                    result_block,
                )
                if matched_call is not None:
                    matched_call.result_block = result_block
                    matched_call.result_record_index = record_index

        for outcome in event.codex_exec_outcomes:
            outer = calls_by_id.get(outcome.outer_call_id)
            if outer is not None and outer.name == "Bash":
                _remove_call_blocks(blocks_by_record, outer)
                calls.pop(id(outer))
                calls_by_id.pop(outcome.outer_call_id, None)
            result = outcome.result
            error = _result_error(result)
            name = "Bash"
            tool_input = {"command": outcome.command}
            use_block = {
                "type": "tool_use",
                "name": name,
                "input": tool_input,
                "id": outcome.identity,
            }
            call = _AdaptedCall(
                record_index,
                name,
                tool_input,
                outcome.identity,
                use_block,
            )
            result_block = {
                "type": "tool_result",
                "tool_use_id": outcome.identity,
                "content": _retained_result(
                    call,
                    _result_content(result.get("output"), result),
                    error,
                ),
                "is_error": error is not None,
            }
            call.result_block = result_block
            call.result_record_index = record_index
            _append_block(blocks_by_record, roles_by_record, record_index, "assistant", use_block)
            _append_block(blocks_by_record, roles_by_record, record_index, "user", result_block)
            calls[id(call)] = call
            calls_by_id[outcome.identity] = call

    item_entries = (
        codex_item_activity(turns) if getattr(parser, "cli_name", None) == "codex" else None
    )
    if item_entries is not None:
        _add_codex_items(
            item_entries,
            calls.values(),
            user_record_indexes,
            blocks_by_record,
            roles_by_record,
        )

    return [
        {
            "type": roles_by_record.get(index, "assistant"),
            "message": {
                "role": roles_by_record.get(index, "assistant"),
                "content": list(blocks.values()),
            },
        }
        for index, blocks in sorted(blocks_by_record.items())
        if blocks
    ]


def _add_codex_items(
    items: list[ToolActivityEntry],
    calls: Iterable[_AdaptedCall],
    user_indexes: list[int],
    blocks_by_record: _RecordBlocks,
    roles_by_record: dict[int, str],
) -> None:
    candidates: dict[_ActivityKey, deque[_AdaptedCall]] = {}
    for call in calls:
        key = _activity_key(
            _turn_owner(call.record_index, user_indexes),
            call.name,
            call.tool_input,
        )
        if key is not None:
            candidates.setdefault(key, deque()).append(call)

    for ordinal, item in enumerate(items):
        owner = _turn_owner(item.record_index, user_indexes)
        key = _activity_key(owner, item.tool_name, item.tool_input)
        queue = candidates.get(key) if key is not None else None
        match = queue.popleft() if queue and _activity_matches(item, queue[0]) else None
        if match is not None:
            _remove_call_blocks(blocks_by_record, match)

        tool_use_id = item.tool_use_id or f"codex-item-{item.record_index}-{ordinal}"
        use_block = {
            "type": "tool_use",
            "name": item.tool_name,
            "input": item.tool_input,
            "id": tool_use_id,
        }
        _append_block(
            blocks_by_record,
            roles_by_record,
            item.record_index,
            "assistant",
            use_block,
        )
        result_block: dict[str, Any] | None = None
        is_file_change = item.tool_name == "apply_patch" and "file_path" in item.tool_input
        if item.resolved and not is_file_change:
            result_block = {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": (
                    _bounded_text(item.error)
                    if item.error is not None
                    else _bounded_text(item.retained_result)
                    if item.retained_result is not None
                    else _bounded_text(item.outcome)
                    if item.outcome is not None
                    else ""
                ),
                "is_error": item.error is not None,
            }
            _append_block(
                blocks_by_record,
                roles_by_record,
                item.record_index,
                "user",
                result_block,
            )
        call = _AdaptedCall(
            item.record_index,
            item.tool_name,
            item.tool_input,
            tool_use_id,
            use_block,
            result_block,
            item.record_index if result_block is not None else None,
        )
        if key is not None:
            candidates.setdefault(key, deque()).append(call)


def _append_block(
    blocks_by_record: _RecordBlocks,
    roles_by_record: dict[int, str],
    record_index: int,
    role: str,
    block: dict[str, Any],
) -> None:
    blocks_by_record.setdefault(record_index, {})[id(block)] = block
    if role == "user" or record_index not in roles_by_record:
        roles_by_record[record_index] = role


def _remove_call_blocks(blocks_by_record: _RecordBlocks, call: _AdaptedCall) -> None:
    bucket = blocks_by_record.get(call.record_index, {})
    bucket.pop(id(call.use_block), None)
    if call.result_block is not None and call.result_record_index is not None:
        result_bucket = blocks_by_record.get(call.result_record_index, {})
        result_bucket.pop(id(call.result_block), None)


def _turn_owner(record_index: int, user_indexes: list[int]) -> int | None:
    position = bisect_right(user_indexes, record_index)
    return user_indexes[position - 1] if position else None


def _activity_key(
    owner: int | None,
    tool_name: str,
    tool_input: dict[str, Any],
) -> _ActivityKey | None:
    if tool_name == "Bash":
        comparable_input: Any = tool_input.get("command")
    elif tool_name.startswith("mcp "):
        comparable_input = tool_input
    else:
        return None
    return (
        owner,
        tool_name,
        _input_key(comparable_input),
    )


def _input_key(value: _JSONInput) -> Hashable:
    """Make JSON containers hashable while preserving Python's input equality."""
    if isinstance(value, dict):
        return frozenset((key, _input_key(item)) for key, item in value.items())
    if isinstance(value, list):
        return tuple(_input_key(item) for item in value)
    return value


def _activity_matches(item: ToolActivityEntry, call: _AdaptedCall) -> bool:
    if item.tool_name != call.name:
        return False
    if item.tool_name == "Bash":
        return item.tool_input.get("command") == call.tool_input.get("command")
    if item.tool_name.startswith("mcp "):
        return item.tool_input == call.tool_input
    return False


def _retained_result(call: _AdaptedCall | None, output: str, error: str | None) -> str:
    if error is not None:
        return _bounded_text(error)
    if call is not None and is_commit_producing(call.name, call.tool_input):
        return _bounded_text(output)
    return ""


def _bounded_text(value: str | None) -> str:
    return escape_ledger_text(value or "")[:_RESULT_TEXT_MAX_CHARS]


def _result_content(content: Any, result: Mapping[str, Any]) -> str:
    direct = _content_text(content)
    if direct:
        return direct
    for key in ("output", "content", "message", "error"):
        value = _content_text(result.get(key))
        if value:
            return value
    return ""


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, Mapping):
        for key in ("text", "output", "content", "message", "error"):
            if key in value:
                nested = _content_text(value[key])
                if nested:
                    return nested
        return json.dumps(dict(value), default=str, sort_keys=True)
    if isinstance(value, list):
        return "\n".join(filter(None, (_content_text(item) for item in value)))
    return str(value)


def _result_error(value: Mapping[str, Any]) -> str | None:
    status = value.get("status")
    failed = (
        status in {"error", "cancelled", "failed"}
        or value.get("is_error") is True
        or value.get("isError") is True
        or value.get("success") is False
        or value.get("error") is not None
    )
    if not failed:
        nested = value.get("output")
        return _result_error(nested) if isinstance(nested, Mapping) else None
    for key in ("error", "message", "output", "content"):
        text = _content_text(value.get(key))
        if text:
            return text
    return str(status or "failed")
