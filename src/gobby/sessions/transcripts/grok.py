"""Grok transcript parser for ``updates.jsonl`` session files."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

from gobby.sessions.transcripts.base import (
    BaseTranscriptParser,
    ParsedMessage,
    ParsedToolEvent,
    TokenUsage,
)

logger = logging.getLogger(__name__)


class GrokTranscriptParser(BaseTranscriptParser):
    """Parse Grok ACP update JSONL into normalized transcript messages."""

    def __init__(
        self,
        session_id: str | None = None,
        logger_instance: logging.Logger | None = None,
    ) -> None:
        super().__init__(cli_name="grok", session_id=session_id, logger_instance=logger_instance)

    def parse_line(self, line: str, index: int) -> ParsedMessage | ParsedToolEvent | None:
        if not line.strip():
            return None
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            self.error_log.log_malformed_line(index, self.session_id, line, str(exc))
            return None
        if not isinstance(data, dict):
            self.error_log.log_malformed_line(index, self.session_id, line, "Line is not an object")
            return None

        timestamp = _parse_timestamp(data)
        update = _extract_update(data)
        if update is None:
            return None

        update_type = str(update.get("sessionUpdate") or update.get("type") or "")
        content = _extract_text(update.get("content"))
        message_id = _message_id("grok", self.session_id, index, update.get("messageId"))

        if update_type == "user_message_chunk":
            return _message(
                index,
                "user",
                content,
                "text",
                timestamp,
                data,
                message_id=message_id,
            )
        if update_type == "agent_message_chunk":
            return _message(
                index,
                "assistant",
                content,
                "text",
                timestamp,
                data,
                message_id=message_id,
            )
        if update_type == "agent_thought_chunk":
            return _message(
                index,
                "assistant",
                content,
                "thinking",
                timestamp,
                data,
                message_id=message_id,
            )
        if update_type == "tool_call":
            tool_name = update.get("title") or update.get("name") or "tool"
            tool_use_id = str(update.get("toolCallId") or _tool_use_id(index, str(tool_name)))
            tool_input = update.get("rawInput") or update.get("input") or {}
            if not isinstance(tool_input, dict):
                tool_input = {"input": tool_input}
            return _message(
                index,
                "assistant",
                f"Tool call: {tool_name}",
                "tool_use",
                timestamp,
                data,
                tool_name=str(tool_name),
                tool_input=tool_input,
                tool_use_id=tool_use_id,
            )
        if update_type == "tool_call_update":
            call_id = str(update.get("toolCallId") or _tool_use_id(index, "tool"))
            result = _extract_tool_result(update)
            return _message(
                index,
                "tool",
                result.get("output", ""),
                "tool_result",
                timestamp,
                data,
                tool_result=result,
                tool_use_id=call_id,
            )
        if update_type == "hook_execution":
            hook_name = str(update.get("hook") or update.get("hookName") or "hook")
            result = {"output": content or json.dumps(update, sort_keys=True), "raw": update}
            return _message(
                index,
                "tool",
                result["output"],
                "tool_result",
                timestamp,
                data,
                tool_name=hook_name,
                tool_result=result,
                tool_use_id=str(update.get("id") or _tool_use_id(index, hook_name)),
            )

        return None

    def extract_last_messages(
        self, turns: list[dict[str, Any]], num_pairs: int = 2
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, str]] = []
        for turn in reversed(turns):
            update = _extract_update(turn)
            if not update:
                continue
            update_type = str(update.get("sessionUpdate") or "")
            if update_type == "user_message_chunk":
                messages.insert(
                    0, {"role": "user", "content": _extract_text(update.get("content"))}
                )
            elif update_type == "agent_message_chunk":
                messages.insert(
                    0,
                    {"role": "assistant", "content": _extract_text(update.get("content"))},
                )
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


def _message(
    index: int,
    role: str,
    content: str,
    content_type: str,
    timestamp: datetime,
    raw_json: dict[str, Any],
    *,
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
    tool_result: dict[str, Any] | None = None,
    tool_use_id: str | None = None,
    message_id: str | None = None,
) -> ParsedMessage:
    return ParsedMessage(
        index=index,
        role=role,
        content=content,
        content_type=content_type,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_result=tool_result,
        timestamp=timestamp,
        raw_json=raw_json,
        usage=TokenUsage(),
        tool_use_id=tool_use_id,
        message_id=message_id,
    )


def _parse_timestamp(data: dict[str, Any]) -> datetime:
    for key in ("timestamp", "createdAt", "created_at"):
        value = data.get(key)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
    return datetime.now(UTC)


def _extract_update(data: dict[str, Any]) -> dict[str, Any] | None:
    params = data.get("params")
    if isinstance(params, dict):
        update = params.get("update")
        if isinstance(update, dict):
            return update
    update = data.get("update")
    return update if isinstance(update, dict) else None


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        nested = content.get("content")
        if nested is not content:
            return _extract_text(nested)
    if isinstance(content, list):
        return "\n".join(part for part in (_extract_text(item) for item in content) if part)
    return ""


def _extract_tool_result(update: dict[str, Any]) -> dict[str, Any]:
    output = _extract_text(update.get("content"))
    error = update.get("error")
    return {"output": output, "error": error, "raw": update}


def _tool_use_id(index: int, tool_name: str) -> str:
    digest = hashlib.sha256(f"grok|{index}|{tool_name}".encode()).hexdigest()[:16]
    return f"grok-tu-{digest}"


def _message_id(cli_name: str, session_id: str | None, index: int, raw_id: Any) -> str:
    prefix = session_id or cli_name
    if isinstance(raw_id, str) and raw_id:
        return f"{prefix}:grok:{index}:{raw_id}"
    return f"{prefix}:grok:{index}"


__all__ = ["GrokTranscriptParser"]
