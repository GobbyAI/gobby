"""Transcript parser for Factory Droid JSONL session files."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Iterator, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from gobby.sessions.transcripts.base import (
    BaseTranscriptParser,
    ParsedAdjustment,
    ParsedMessage,
    ParsedToolEvent,
    ParseEvent,
    RawLine,
    TokenUsage,
    _unknown_block_message,
    annotate_record_source,
)
from gobby.sessions.transcripts.tool_activity import event_activity_by_user_index

logger = logging.getLogger(__name__)

_INJECTED_BLOCK_PATTERN = re.compile(
    r"<(system-reminder|command-name|command-message)>.*?</\1>",
    re.DOTALL,
)


def _strip_injected_blocks(text: str) -> str:
    """Remove injected system/command blocks from user-visible text."""
    return _INJECTED_BLOCK_PATTERN.sub("", text).strip()


def _parse_timestamp(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(UTC)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


def _coerce_token_count(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _usage_state(usage: TokenUsage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
    }


def _usage_from_state(value: Any) -> TokenUsage | None:
    if not isinstance(value, dict):
        return None
    return TokenUsage(
        input_tokens=_coerce_token_count(value.get("input_tokens")),
        output_tokens=_coerce_token_count(value.get("output_tokens")),
        cache_creation_tokens=_coerce_token_count(value.get("cache_creation_tokens")),
        cache_read_tokens=_coerce_token_count(value.get("cache_read_tokens")),
    )


def _usage_delta(current: TokenUsage, previous: TokenUsage | None) -> TokenUsage:
    if previous is None:
        return current
    return TokenUsage(
        input_tokens=max(0, current.input_tokens - previous.input_tokens),
        output_tokens=max(0, current.output_tokens - previous.output_tokens),
        cache_creation_tokens=max(
            0, current.cache_creation_tokens - previous.cache_creation_tokens
        ),
        cache_read_tokens=max(0, current.cache_read_tokens - previous.cache_read_tokens),
    )


def _usage_has_tokens(usage: TokenUsage) -> bool:
    return any(_usage_state(usage).values())


def _todo_state_tool_use_id(session_id: str | None, todos: list[Any]) -> str:
    payload = json.dumps(
        {"session_id": session_id or "", "todos": todos},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"droid-todo-state-{digest}"


class DroidTranscriptParser(BaseTranscriptParser):
    """Parse Factory Droid v0.106.0+ JSONL transcripts."""

    def __init__(
        self,
        *,
        session_id: str | None = None,
        transcript_path: Path | str | None = None,
        logger_instance: logging.Logger | None = None,
    ) -> None:
        super().__init__(
            cli_name="droid",
            session_id=session_id,
            logger_instance=logger_instance,
            transcript_path=transcript_path,
        )
        self._sidecar_usage: TokenUsage | None = None
        self._sidecar_model: str | None = None
        self._last_emitted_usage: TokenUsage | None = None
        self._last_assistant_index: int | None = None

    def snapshot_state(self) -> dict[str, Any]:
        if self._last_emitted_usage is None:
            return {}
        return {"last_emitted_usage": _usage_state(self._last_emitted_usage)}

    def hydrate_state(self, state: Mapping[str, Any]) -> None:
        self._last_emitted_usage = _usage_from_state(state.get("last_emitted_usage"))

    def _load_sidecar(self, jsonl_path: Path) -> None:
        """Side-read <droid-uuid>.settings.json beside the JSONL transcript."""
        sidecar_path = jsonl_path.with_suffix(".settings.json")
        self._sidecar_usage = None
        self._sidecar_model = None

        try:
            raw = sidecar_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            logger.debug("Droid sidecar not present at %s", sidecar_path)
            return
        except OSError as exc:
            logger.warning("Droid sidecar read error at %s: %s", sidecar_path, exc)
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Droid sidecar parse error at %s: %s", sidecar_path, exc)
            return
        if not isinstance(data, dict):
            return

        usage_raw = data.get("tokenUsage")
        if isinstance(usage_raw, dict):
            self._sidecar_usage = TokenUsage(
                input_tokens=_coerce_token_count(usage_raw.get("inputTokens")),
                output_tokens=(
                    _coerce_token_count(usage_raw.get("outputTokens"))
                    + _coerce_token_count(usage_raw.get("thinkingTokens"))
                ),
                cache_creation_tokens=_coerce_token_count(usage_raw.get("cacheCreationTokens")),
                cache_read_tokens=_coerce_token_count(usage_raw.get("cacheReadTokens")),
            )
        model = data.get("model")
        self._sidecar_model = model if isinstance(model, str) else None

    def _expand_line(self, line: str, index: int) -> list[ParsedMessage]:
        """Expand one Droid JSONL line into zero or more ParsedMessage records."""
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            self.error_log.log_decode_failure(index, self.session_id, line, exc)
            return []
        if not isinstance(record, dict):
            self.error_log.log_decode_failure(index, self.session_id, line, None)
            return []

        timestamp_raw = record.get("timestamp")
        timestamp = _parse_timestamp(timestamp_raw if isinstance(timestamp_raw, str) else None)
        message_id_raw = record.get("id")
        message_id = message_id_raw if isinstance(message_id_raw, str) else None
        record_type = record.get("type")
        if record_type == "session_start":
            session_title = record.get("title")
            if isinstance(session_title, str) and session_title.strip():
                return [
                    ParsedMessage(
                        index=index,
                        role="system",
                        content=session_title,
                        content_type="session_title",
                        tool_name=None,
                        tool_input=None,
                        tool_result=None,
                        timestamp=timestamp,
                        raw_json=record,
                        message_id=message_id,
                        model=self._sidecar_model,
                    )
                ]
            return []
        if record_type == "todo_state":
            todos = record.get("todos")
            todos_payload = todos if isinstance(todos, list) else []
            todo_tool_use_id = _todo_state_tool_use_id(self.session_id, todos_payload)
            return [
                ParsedMessage(
                    index=index,
                    role="assistant",
                    content="",
                    content_type="tool_use",
                    tool_name="TodoWrite",
                    tool_input={"todos": todos_payload},
                    tool_result=None,
                    tool_use_id=todo_tool_use_id,
                    timestamp=timestamp,
                    raw_json=record,
                    message_id=message_id,
                    model=self._sidecar_model,
                ),
                ParsedMessage(
                    index=index,
                    role="tool",
                    content="",
                    content_type="tool_result",
                    tool_name=None,
                    tool_input=None,
                    tool_result={"todos": todos_payload, "source": "todo_state"},
                    tool_use_id=todo_tool_use_id,
                    timestamp=timestamp,
                    raw_json=record,
                    message_id=message_id,
                    model=self._sidecar_model,
                ),
            ]
        if record_type != "message":
            record_block_type = str(record_type or "<missing>")
            return [
                _unknown_block_message(
                    index=index,
                    block_type=record_block_type,
                    raw=record,
                    timestamp=timestamp,
                    message_id=message_id,
                    model=self._sidecar_model,
                )
            ]

        message = record.get("message") or {}
        if not isinstance(message, dict):
            return []

        role = message.get("role")
        role = role if isinstance(role, str) else "unknown"
        content_blocks = message.get("content") or []
        if not isinstance(content_blocks, list):
            return []

        out: list[ParsedMessage] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            raw_block_type = block.get("type")

            if raw_block_type == "text":
                raw_text = block.get("text")
                if not isinstance(raw_text, str):
                    continue
                text = _strip_injected_blocks(raw_text) if role == "user" else raw_text
                if not text:
                    continue
                out.append(
                    ParsedMessage(
                        index=index,
                        role=role,
                        content=text,
                        content_type="text",
                        tool_name=None,
                        tool_input=None,
                        tool_result=None,
                        timestamp=timestamp,
                        raw_json=record,
                        message_id=message_id,
                        model=self._sidecar_model,
                    )
                )
            elif raw_block_type == "thinking":
                thinking = block.get("thinking")
                if not isinstance(thinking, str) or not thinking.strip():
                    continue
                out.append(
                    ParsedMessage(
                        index=index,
                        role=role,
                        content=thinking,
                        content_type="thinking",
                        tool_name=None,
                        tool_input=None,
                        tool_result=None,
                        timestamp=timestamp,
                        raw_json=record,
                        message_id=message_id,
                        model=self._sidecar_model,
                    )
                )
            elif raw_block_type == "tool_use":
                tool_input = block.get("input")
                tool_name = block.get("name")
                tool_use_id = block.get("id")
                out.append(
                    ParsedMessage(
                        index=index,
                        role=role,
                        content="",
                        content_type="tool_use",
                        tool_name=tool_name if isinstance(tool_name, str) else None,
                        tool_input=tool_input if isinstance(tool_input, dict) else {},
                        tool_result=None,
                        tool_use_id=tool_use_id if isinstance(tool_use_id, str) else None,
                        timestamp=timestamp,
                        raw_json=record,
                        message_id=message_id,
                        model=self._sidecar_model,
                    )
                )
            elif raw_block_type == "tool_result":
                content_value = block.get("content")
                is_error = bool(block.get("is_error"))
                result_payload = {"content": content_value, "is_error": is_error}
                tool_use_id = block.get("tool_use_id")
                out.append(
                    ParsedMessage(
                        index=index,
                        role=role,
                        content="",
                        content_type="tool_result",
                        tool_name=None,
                        tool_input=None,
                        tool_result=result_payload,
                        tool_use_id=tool_use_id if isinstance(tool_use_id, str) else None,
                        timestamp=timestamp,
                        raw_json=record,
                        message_id=message_id,
                        model=self._sidecar_model,
                    )
                )
            else:
                block_type = str(raw_block_type or "<missing>")
                out.append(
                    _unknown_block_message(
                        index=index,
                        block_type=block_type,
                        raw=block,
                        role=role,
                        timestamp=timestamp,
                        message_id=message_id,
                        model=self._sidecar_model,
                    )
                )

        return out

    def parse_line(self, line: str, index: int) -> ParsedMessage | ParsedToolEvent | None:
        """Return the first expanded block for single-record callers."""
        expanded = self._expand_line(line, index)
        return expanded[0] if expanded else None

    def iter_parse_events(
        self, raw_lines: Iterable[RawLine], start_index: int = 0
    ) -> Iterator[ParseEvent]:
        """Stream events with per-ParsedMessage indexing; track the last assistant
        message so :meth:`finalize` can apply sidecar token usage to it (Droid's
        post-pass mutation). No forward lookahead, so every event is ``parser_safe``.
        """
        if self._transcript_path is not None:
            self._load_sidecar(self._transcript_path)
        self._last_assistant_index = None

        current_index = start_index
        for raw in raw_lines:
            if not raw.text.strip():
                continue
            start_idx = current_index
            expanded = self._expand_line(raw.text, current_index)
            for msg in expanded:
                msg.index = current_index
                raw_type = msg.raw_json.get("type") if isinstance(msg.raw_json, dict) else None
                if (
                    isinstance(msg, ParsedMessage)
                    and msg.role == "assistant"
                    and raw_type != "todo_state"
                ):
                    self._last_assistant_index = current_index
                current_index += 1
            if expanded:
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

    def finalize(self) -> list[ParsedAdjustment]:
        """Assign new sidecar token usage to the last assistant message."""
        last_assistant_index = getattr(self, "_last_assistant_index", None)
        if self._sidecar_usage is not None and last_assistant_index is not None:
            delta = _usage_delta(self._sidecar_usage, self._last_emitted_usage)
            self._last_emitted_usage = self._sidecar_usage
            if _usage_has_tokens(delta):
                return [ParsedAdjustment(last_assistant_index, "usage", delta)]
        return []

    def extract_last_messages(
        self,
        turns: list[dict[str, Any]],
        num_pairs: int = 2,
        *,
        include_tool_activity: bool = False,
    ) -> list[dict[str, Any]]:
        """Return the last N user/assistant text turns in chronological order."""
        messages: list[dict[str, Any]] = []
        activity = event_activity_by_user_index(self, turns) if include_tool_activity else {}
        for turn_index in range(len(turns) - 1, -1, -1):
            turn = turns[turn_index]
            if turn.get("type") != "message":
                continue
            message = turn.get("message") or {}
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue

            content_blocks = message.get("content") or []
            if not isinstance(content_blocks, list):
                continue

            text_parts: list[str] = []
            for block in content_blocks:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                raw_text = block.get("text")
                if not isinstance(raw_text, str):
                    continue
                text = _strip_injected_blocks(raw_text) if role == "user" else raw_text
                if text:
                    text_parts.append(text)

            if not text_parts:
                continue
            extracted: dict[str, Any] = {"role": role, "content": "\n\n".join(text_parts)}
            if role == "user" and turn_index in activity:
                extracted["tool_activity"] = activity[turn_index]
            messages.append(extracted)
            if len(messages) >= num_pairs * 2:
                break

        return list(reversed(messages))

    def extract_turns_since_clear(
        self, turns: list[dict[str, Any]], max_turns: int | None = None
    ) -> list[dict[str, Any]]:
        """Droid has no in-file clear boundary."""
        if max_turns is None:
            return list(turns)
        return list(turns)[-max_turns:]

    def is_session_boundary(self, turn: dict[str, Any]) -> bool:
        """Every Droid session is its own JSONL file."""
        return False
