"""Transcript source detection helpers."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.storage.session_models import Session

logger = logging.getLogger(__name__)


def _detect_source_from_path(path: str | None) -> str | None:
    """Infer transcript source from a known path shape."""
    from gobby.sessions.transcript_paths import detect_source_from_path

    return detect_source_from_path(path)


def _load_json_object(raw: str) -> dict[str, Any] | None:
    """Parse a JSON object, returning None for non-dict or invalid values."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _detect_source_from_record(data: dict[str, Any]) -> str | None:
    """Infer transcript source from a decoded transcript record."""
    params = data.get("params")
    if isinstance(params, dict):
        update = params.get("update")
        if isinstance(update, dict) and "sessionUpdate" in update:
            return "grok"

    line_type = data.get("type")
    payload = data.get("payload")
    message = data.get("message")

    if line_type == "session_start" and data.get("version") == 2:
        return "droid"

    if isinstance(payload, dict) and line_type in {
        "response_item",
        "event_msg",
        "session_meta",
        "turn_context",
    }:
        return "codex"

    if isinstance(message, dict):
        if isinstance(message.get("parts"), list):
            return "qwen"
        if "role" in message:
            return "claude"
        if "content" in message:
            return "claude"

    if line_type in {"assistant", "summary", "system"}:
        return "claude"
    if line_type == "user":
        return "claude" if isinstance(message, dict) else "qwen"

    if line_type in {"init", "message", "tool_use", "tool_result", "result", "model"}:
        return "qwen"

    return None


def _detect_source_from_jsonl_lines(lines: list[str]) -> str | None:
    """Infer transcript source from JSONL content."""
    for raw_line in lines:
        if not raw_line.strip():
            continue
        data = _load_json_object(raw_line)
        if not data:
            continue
        detected = _detect_source_from_record(data)
        if detected:
            return detected
    return None


def _resolve_effective_source(
    session: Session,
    *,
    transcript_path: str | None = None,
    lines: list[str] | None = None,
    session_id: str | None = None,
) -> tuple[str, str | None]:
    """Choose the parser source from transcript evidence, with DB source as fallback."""
    detected_source = _detect_source_from_path(transcript_path)
    if detected_source is None and lines is not None:
        detected_source = _detect_source_from_jsonl_lines(lines)

    effective_source = detected_source or session.source or "claude"
    stored_source = getattr(session, "source", None)
    if detected_source and stored_source and detected_source != stored_source:
        logger.warning(
            "Transcript source mismatch for session %s: stored=%s detected=%s path=%s",
            session_id,
            stored_source,
            detected_source,
            transcript_path,
        )

    return effective_source, detected_source
