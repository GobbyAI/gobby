"""Canonicalize provider-specific transcript records before consumption."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import replace
from typing import Any, cast

from gobby.sessions.transcripts.base import ParsedMessage, ParsedToolEvent

logger = logging.getLogger(__name__)

TranscriptRecord = ParsedMessage | ParsedToolEvent

MAX_TEXT_COLLECT_DEPTH = 50
TRUNCATED_TEXT_MARKER = "[truncated]"
_DROID_TODO_STATE_TOOL_USE_ID_PREFIX = "droid-todo-state-"

_SUCCESS_VALUES = frozenset(
    {"complete", "completed", "ok", "pass", "passed", "success", "successful", "succeeded"}
)
_FAILURE_VALUES = frozenset(
    {"blocked", "cancelled", "canceled", "denied", "error", "errored", "failed", "failure"}
)


def normalize_transcript_records(
    records: Iterable[TranscriptRecord], source: str | None
) -> list[TranscriptRecord]:
    """Return canonical transcript records safe for rendering, indexing, and stats."""
    if source == "droid":
        return _collapse_droid_todo_state_snapshots(records)

    if source != "grok":
        return list(records)

    normalized: list[TranscriptRecord] = []
    for record in records:
        if not isinstance(record, ParsedMessage):
            normalized.append(record)
            continue
        update = _grok_hook_execution_update(record)
        if update is None:
            normalized.append(record)
            continue
        feedback = _grok_hook_feedback_text(record, update)
        if feedback is None:
            logger.debug(
                "Dropped Grok hook execution feedback record with no user-visible output",
                extra={
                    "record_index": record.index,
                    "tool_name": record.tool_name,
                    "update_type": _update_type(update),
                },
            )
            continue
        normalized.append(
            replace(
                record,
                role="system",
                content=feedback,
                content_type="text",
                tool_name=None,
                tool_input=None,
                tool_result=None,
                tool_use_id=None,
            )
        )
    return normalized


def _collapse_droid_todo_state_snapshots(
    records: Iterable[TranscriptRecord],
) -> list[TranscriptRecord]:
    items = list(records)
    normalized: list[TranscriptRecord] = []
    previous_snapshot_id: str | None = None
    index = 0
    while index < len(items):
        snapshot_id = _droid_todo_state_snapshot_id(items, index)
        if snapshot_id is None:
            normalized.append(items[index])
            previous_snapshot_id = None
            index += 1
            continue
        if snapshot_id != previous_snapshot_id:
            normalized.extend((items[index], items[index + 1]))
        previous_snapshot_id = snapshot_id
        index += 2
    return normalized


def _droid_todo_state_snapshot_id(records: list[TranscriptRecord], index: int) -> str | None:
    if index + 1 >= len(records):
        return None
    tool_use = records[index]
    tool_result = records[index + 1]
    if not isinstance(tool_use, ParsedMessage) or not isinstance(tool_result, ParsedMessage):
        return None
    if tool_use.content_type != "tool_use" or tool_use.tool_name != "TodoWrite":
        return None
    tool_use_id = tool_use.tool_use_id
    if not tool_use_id or not tool_use_id.startswith(_DROID_TODO_STATE_TOOL_USE_ID_PREFIX):
        return None
    if tool_result.content_type != "tool_result" or tool_result.tool_use_id != tool_use_id:
        return None
    if not isinstance(tool_result.tool_result, dict):
        return None
    if tool_result.tool_result.get("source") != "todo_state":
        return None
    return tool_use_id


def _grok_hook_execution_update(record: ParsedMessage) -> dict[str, Any] | None:
    if record.content_type != "tool_result":
        return None
    candidates: list[Any] = []
    if isinstance(record.tool_result, dict):
        candidates.append(record.tool_result.get("raw"))
    candidates.append(record.raw_json)
    for candidate in candidates:
        update = _extract_grok_update(candidate)
        if update and _update_type(update) == "hook_execution":
            return update
    return None


def _extract_grok_update(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    params = value.get("params")
    if isinstance(params, dict) and isinstance(params.get("update"), dict):
        return cast("dict[str, Any]", params["update"])
    update = value.get("update")
    if isinstance(update, dict):
        return cast("dict[str, Any]", update)
    if _update_type(value):
        return value
    return None


def _update_type(update: dict[str, Any]) -> str:
    for key in ("sessionUpdate", "type"):
        value = update.get(key)
        if isinstance(value, str):
            return value
    return ""


def _grok_hook_feedback_text(record: ParsedMessage, update: dict[str, Any]) -> str | None:
    output, truncated = _hook_output_text(update)
    if truncated:
        logger.warning(
            "Grok hook execution output exceeded collection depth; appended truncation marker",
            extra={
                "record_index": record.index,
                "tool_name": record.tool_name,
                "update_type": _update_type(update),
            },
        )
    if output:
        return _append_truncated_marker(output, truncated)
    if _hook_succeeded(update):
        return None

    hook_name = _first_text(update.get("hook"), update.get("hookName"), record.tool_name) or "hook"
    status = _first_text(update.get("status"), update.get("state"), update.get("outcome"))
    if status:
        return _append_truncated_marker(f"{hook_name} hook execution: {status}", truncated)
    return _append_truncated_marker(
        f"{hook_name} hook execution produced no success status",
        truncated,
    )


def _hook_succeeded(update: dict[str, Any]) -> bool:
    for key in ("success", "ok", "passed"):
        value = update.get(key)
        if isinstance(value, bool):
            return value

    status = _first_text(update.get("status"), update.get("state"), update.get("outcome"))
    if status:
        lowered = status.strip().lower()
        if lowered in _SUCCESS_VALUES:
            return True
        if lowered in _FAILURE_VALUES:
            return False

    for key in ("exitCode", "exit_code", "code"):
        value = update.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            return int(value) == 0
        except (TypeError, ValueError):
            continue
    return False


def _hook_output_text(update: dict[str, Any]) -> tuple[str, bool]:
    parts: list[str] = []
    truncated = False
    for key in ("output", "stdout", "stderr", "message", "error", "content"):
        truncated = _collect_text(update.get(key), parts) or truncated
    result = update.get("result")
    if isinstance(result, dict):
        for key in ("output", "stdout", "stderr", "message", "error", "content"):
            truncated = _collect_text(result.get(key), parts) or truncated
    return "\n".join(part for part in parts if part).strip(), truncated


def _collect_text(value: Any, parts: list[str], depth: int = 0) -> bool:
    if value is None:
        return False
    # Grok hook payloads can include recursive/nested tool data; cap traversal
    # so malformed payloads cannot exhaust recursion while extracting feedback.
    if depth > MAX_TEXT_COLLECT_DEPTH:
        return True
    if isinstance(value, str):
        text = value.strip()
        if text:
            parts.append(text)
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        parts.append(str(value))
        return False
    truncated = False
    if isinstance(value, list):
        for item in value:
            truncated = _collect_text(item, parts, depth + 1) or truncated
        return truncated
    if isinstance(value, dict):
        for key in ("text", "output", "stdout", "stderr", "message", "error", "content"):
            truncated = _collect_text(value.get(key), parts, depth + 1) or truncated
    return truncated


def _append_truncated_marker(text: str, truncated: bool) -> str:
    if not truncated:
        return text
    return f"{text}\n{TRUNCATED_TEXT_MARKER}"


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
