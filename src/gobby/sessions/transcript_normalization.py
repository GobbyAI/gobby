"""Canonicalize provider-specific transcript records before consumption."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Any, cast

from gobby.sessions.transcripts.base import ParsedMessage, ParsedToolEvent

TranscriptRecord = ParsedMessage | ParsedToolEvent

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
    return str(update.get("sessionUpdate") or update.get("type") or "")


def _grok_hook_feedback_text(record: ParsedMessage, update: dict[str, Any]) -> str | None:
    output = _hook_output_text(update)
    if output:
        return output
    if _hook_succeeded(update):
        return None

    hook_name = _first_text(update.get("hook"), update.get("hookName"), record.tool_name) or "hook"
    status = _first_text(update.get("status"), update.get("state"), update.get("outcome"))
    if status:
        return f"{hook_name} hook execution: {status}"
    return f"{hook_name} hook execution produced no success status"


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


def _hook_output_text(update: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("output", "stdout", "stderr", "message", "error", "content"):
        _collect_text(update.get(key), parts)
    result = update.get("result")
    if isinstance(result, dict):
        for key in ("output", "stdout", "stderr", "message", "error", "content"):
            _collect_text(result.get(key), parts)
    return "\n".join(part for part in parts if part).strip()


def _collect_text(value: Any, parts: list[str]) -> None:
    if value is None:
        return
    if isinstance(value, str):
        text = value.strip()
        if text:
            parts.append(text)
        return
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        parts.append(str(value))
        return
    if isinstance(value, list):
        for item in value:
            _collect_text(item, parts)
        return
    if isinstance(value, dict):
        for key in ("text", "output", "stdout", "stderr", "message", "error", "content"):
            _collect_text(value.get(key), parts)


def _first_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
