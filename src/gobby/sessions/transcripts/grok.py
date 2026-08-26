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
    _unknown_block_message,
)
from gobby.sessions.transcripts.tool_activity import (
    ToolActivityEntry,
    canonical_tool_name,
    commit_outcome,
    is_commit_producing,
    render_tool_activity,
)

logger = logging.getLogger(__name__)

_SUPPRESSED_UPDATE_TYPES = frozenset(
    {
        "retry_state",
        "compaction_checkpoint",
        "auto_compact_completed",
        "task_backgrounded",
        "task_completed",
        "current_mode_update",
        "hook_annotation",
    }
)
_PAIR_RESPONSE_CHAR_BUDGET = 4000
_TURN_COMPLETED_UPDATE = "turn_completed"
_USER_MESSAGE_CHUNK = "user_message_chunk"
_AGENT_MESSAGE_CHUNK = "agent_message_chunk"


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
            self.error_log.log_decode_failure(index, self.session_id, line, exc)
            return None
        if not isinstance(data, dict):
            self.error_log.log_decode_failure(index, self.session_id, line, None)
            return None

        timestamp = _parse_timestamp(data)
        update = _extract_update(data)
        if update is None:
            return None

        update_type = str(update.get("sessionUpdate") or update.get("type") or "")
        content = _extract_text(update.get("content"))
        message_id = _message_id("grok", self.session_id, index, update.get("messageId"))
        usage = _extract_usage(update)

        if update_type == "turn_completed":
            return _message(
                index,
                "assistant",
                "",
                "turn_completed",
                timestamp,
                data,
                message_id=_message_id("grok", self.session_id, index, update.get("prompt_id")),
                usage=_turn_usage(update),
            )
        if update_type in _SUPPRESSED_UPDATE_TYPES:
            return None
        if update_type == "user_message_chunk":
            return _message(
                index,
                "user",
                content,
                "text",
                timestamp,
                data,
                message_id=message_id,
                usage=usage,
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
                usage=usage,
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
                usage=usage,
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
                usage=usage,
            )
        if update_type == "tool_call_update":
            if update.get("status") not in {"completed", "failed"}:
                return None
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
                usage=usage,
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
                usage=usage,
            )

        block_type = update_type or "<missing>"
        return _unknown_block_message(
            index=index,
            block_type=block_type,
            raw=update,
            timestamp=timestamp,
            message_id=message_id,
            usage=usage,
        )

    def extract_last_messages(
        self,
        turns: list[dict[str, Any]],
        num_pairs: int = 2,
        *,
        include_tool_activity: bool = False,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for segment in reversed(_turn_segments(turns)):
            messages = (
                _segment_pair_messages(segment, include_tool_activity=include_tool_activity)
                + messages
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


def _turn_segments(turns: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    segments: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for turn in turns:
        current.append(turn)
        update = _extract_update(turn)
        kind = (
            str(update.get("sessionUpdate") or update.get("type") or "")
            if update is not None
            else ""
        )
        if kind == _TURN_COMPLETED_UPDATE:
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    return segments


def _segment_pair_messages(
    segment: list[dict[str, Any]], *, include_tool_activity: bool = False
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    accumulated = ""
    pending_user = False
    current_user: dict[str, Any] | None = None
    activity: list[ToolActivityEntry] = []
    activity_by_id: dict[str, ToolActivityEntry] = {}

    def attach_activity() -> None:
        if include_tool_activity and current_user is not None and activity:
            current_user["tool_activity"] = render_tool_activity(activity)

    def flush(*, empty_if_pending: bool = False) -> None:
        nonlocal accumulated, pending_user
        if accumulated:
            messages.append({"role": "assistant", "content": accumulated})
            accumulated = ""
            pending_user = False
        elif empty_if_pending and pending_user:
            messages.append({"role": "assistant", "content": ""})
            pending_user = False

    for record in segment:
        update = _extract_update(record)
        if update is None:
            continue
        update_type = str(update.get("sessionUpdate") or "")
        if update_type == _USER_MESSAGE_CHUNK:
            attach_activity()
            flush()
            current_user = {"role": "user", "content": _extract_text(update.get("content"))}
            messages.append(current_user)
            activity = []
            activity_by_id = {}
            pending_user = True
        elif update_type == _AGENT_MESSAGE_CHUNK:
            text = _extract_text(update.get("content"))
            if not text:
                continue
            remaining = _PAIR_RESPONSE_CHAR_BUDGET - len(accumulated)
            if remaining <= 0:
                continue
            accumulated += text[:remaining]
            pending_user = False
        elif include_tool_activity and update_type == "tool_call" and current_user is not None:
            name, tool_input = canonical_tool_name(update.get("title"), update.get("rawInput"))
            tool_use_id = update.get("toolCallId")
            entry = ToolActivityEntry(
                name,
                tool_input,
                tool_use_id=tool_use_id if isinstance(tool_use_id, str) else None,
            )
            activity.append(entry)
            if entry.tool_use_id:
                activity_by_id[entry.tool_use_id] = entry
        elif include_tool_activity and update_type == "tool_call_update":
            tool_use_id = update.get("toolCallId")
            completed_entry = activity_by_id.get(
                tool_use_id if isinstance(tool_use_id, str) else ""
            )
            status = update.get("status")
            if completed_entry is None or status not in {"completed", "failed"}:
                continue
            completed_entry.resolved = True
            output = _extract_tool_result(update).get("output")
            output_text = str(output) if output is not None else ""
            if status == "failed":
                completed_entry.error = output_text or "failed"
            elif is_commit_producing(completed_entry.tool_name, completed_entry.tool_input):
                completed_entry.outcome = commit_outcome(
                    completed_entry.tool_name, completed_entry.tool_input, output_text
                )
    attach_activity()
    flush(empty_if_pending=True)
    return messages


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
    usage: TokenUsage | None = None,
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
        usage=usage,
        tool_use_id=tool_use_id,
        message_id=message_id,
    )


def _turn_usage(update: dict[str, Any]) -> TokenUsage | None:
    usage = update.get("usage")
    if not isinstance(usage, dict):
        return None
    cache_read = _count(usage.get("cachedReadTokens"))
    cache_creation = _count(usage.get("cacheCreationTokens"))
    input_tokens = max(0, _count(usage.get("inputTokens")) - cache_read - cache_creation)
    output_tokens = _count(usage.get("outputTokens"))
    if input_tokens == output_tokens == cache_read == cache_creation == 0:
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
    )


def _extract_usage(update: dict[str, Any]) -> TokenUsage | None:
    usage = update.get("usage") or update.get("tokenUsage") or update.get("token_usage")
    if not isinstance(usage, dict):
        usage = update

    input_tokens = _count(
        usage.get("input_tokens")
        or usage.get("inputTokens")
        or usage.get("prompt_tokens")
        or usage.get("promptTokens")
    )
    output_tokens = _count(
        usage.get("output_tokens")
        or usage.get("outputTokens")
        or usage.get("completion_tokens")
        or usage.get("completionTokens")
    )
    cache_read = _count(
        usage.get("cache_read_tokens")
        or usage.get("cacheReadTokens")
        or usage.get("cached_input_tokens")
        or usage.get("cachedInputTokens")
    )
    cache_read += _detail_count(
        usage,
        detail_keys=(
            "input_token_details",
            "inputTokenDetails",
            "input_tokens_details",
            "inputTokensDetails",
            "prompt_token_details",
            "promptTokenDetails",
            "prompt_tokens_details",
            "promptTokensDetails",
        ),
        token_keys=(
            "cache_read_tokens",
            "cacheReadTokens",
            "cached_input_tokens",
            "cachedInputTokens",
            "cached_tokens",
            "cachedTokens",
        ),
    )
    cache_creation = _count(
        usage.get("cache_creation_tokens")
        or usage.get("cacheCreationTokens")
        or usage.get("cache_creation_input_tokens")
        or usage.get("cacheCreationInputTokens")
    )
    cache_creation += _detail_count(
        usage,
        detail_keys=(
            "input_token_details",
            "inputTokenDetails",
            "input_tokens_details",
            "inputTokensDetails",
            "prompt_token_details",
            "promptTokenDetails",
            "prompt_tokens_details",
            "promptTokensDetails",
        ),
        token_keys=(
            "cache_creation_tokens",
            "cacheCreationTokens",
            "cache_creation_input_tokens",
            "cacheCreationInputTokens",
        ),
    )
    cache_creation += _numeric_detail_total(
        usage,
        (
            "cache_creation_input_token_details",
            "cacheCreationInputTokenDetails",
            "cache_creation_input_tokens_details",
            "cacheCreationInputTokensDetails",
        ),
    )
    if input_tokens == output_tokens == cache_read == cache_creation == 0:
        return None
    return TokenUsage(
        input_tokens=max(0, input_tokens - cache_read - cache_creation),
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
    )


def _detail_count(
    usage: dict[str, Any], *, detail_keys: tuple[str, ...], token_keys: tuple[str, ...]
) -> int:
    total = 0
    for detail_key in detail_keys:
        details = usage.get(detail_key)
        if not isinstance(details, dict):
            continue
        for token_key in token_keys:
            total += _count(details.get(token_key))
    return total


def _numeric_detail_total(usage: dict[str, Any], detail_keys: tuple[str, ...]) -> int:
    total = 0
    for detail_key in detail_keys:
        details = usage.get(detail_key)
        if not isinstance(details, dict):
            continue
        for value in details.values():
            total += _count(value)
    return total


def _count(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


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
