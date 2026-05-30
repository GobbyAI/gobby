"""Transcript parser for Factory Droid JSONL session files."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
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
)

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


class DroidTranscriptParser(BaseTranscriptParser):
    """Parse Factory Droid v0.106.0+ JSONL transcripts."""

    def __init__(
        self,
        *,
        session_id: str | None = None,
        transcript_path: Path | str | None = None,
        logger_instance: logging.Logger | None = None,
    ) -> None:
        super().__init__(cli_name="droid", session_id=session_id, logger_instance=logger_instance)
        self._transcript_path: Path | None = Path(transcript_path) if transcript_path else None
        self._sidecar_usage: TokenUsage | None = None
        self._sidecar_model: str | None = None
        self._sidecar_loaded_for: Path | None = None

    def _load_sidecar(self, jsonl_path: Path) -> None:
        """Side-read <droid-uuid>.settings.json beside the JSONL transcript."""
        if self._sidecar_loaded_for == jsonl_path and (
            self._sidecar_usage is not None or self._sidecar_model is not None
        ):
            return

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

        self._sidecar_loaded_for = jsonl_path

        usage_raw = data.get("tokenUsage") or {}
        if not isinstance(usage_raw, dict):
            usage_raw = {}

        self._sidecar_usage = TokenUsage(
            input_tokens=_coerce_token_count(usage_raw.get("inputTokens")),
            output_tokens=_coerce_token_count(usage_raw.get("outputTokens")),
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
            self.error_log.log_malformed_line(index, self.session_id, line, str(exc))
            return []
        if not isinstance(record, dict):
            self.error_log.log_unknown_block(index, self.session_id, "<non-object>", {})
            return []

        record_type = record.get("type")
        if record_type == "session_start":
            return []
        if record_type != "message":
            self.error_log.log_unknown_block(
                index,
                self.session_id,
                str(record_type or "<missing>"),
                record,
            )
            return []

        message = record.get("message") or {}
        if not isinstance(message, dict):
            return []

        role = message.get("role")
        role = role if isinstance(role, str) else "unknown"
        content_blocks = message.get("content") or []
        if not isinstance(content_blocks, list):
            return []

        timestamp_raw = record.get("timestamp")
        timestamp = _parse_timestamp(timestamp_raw if isinstance(timestamp_raw, str) else None)
        message_id_raw = record.get("id")
        message_id = message_id_raw if isinstance(message_id_raw, str) else None

        out: list[ParsedMessage] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")

            if block_type == "text":
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
            elif block_type == "thinking":
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
            elif block_type == "tool_use":
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
            elif block_type == "tool_result":
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
                self.error_log.log_unknown_block(
                    index,
                    self.session_id,
                    str(block_type or "<missing>"),
                    block,
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
                if isinstance(msg, ParsedMessage) and msg.role == "assistant":
                    self._last_assistant_index = current_index
                current_index += 1
            if expanded:
                yield ParseEvent(
                    byte_offset=raw.byte_offset,
                    raw_line_no=raw.raw_line_no,
                    parsed_index=start_idx,
                    records=list(expanded),
                    parser_safe=True,
                )

    def finalize(self) -> list[ParsedAdjustment]:
        """Assign sidecar token usage to the last assistant message (post-pass)."""
        last_assistant_index = getattr(self, "_last_assistant_index", None)
        if self._sidecar_usage is not None and last_assistant_index is not None:
            return [ParsedAdjustment(last_assistant_index, "usage", self._sidecar_usage)]
        return []

    def extract_last_messages(
        self, turns: list[dict[str, Any]], num_pairs: int = 2
    ) -> list[dict[str, Any]]:
        """Return the last N user/assistant text turns in chronological order."""
        messages: list[dict[str, Any]] = []
        for turn in reversed(turns):
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
            messages.append({"role": role, "content": "\n\n".join(text_parts)})
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
