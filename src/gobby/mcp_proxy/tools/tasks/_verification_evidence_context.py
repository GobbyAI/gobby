"""Formatting helpers for close-task verification evidence context."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

_EVIDENCE_FIELDS: tuple[str, ...] = (
    "evidence_type",
    "command",
    "exit_code",
    "success",
    "outcome_provenance",
    "output",
    "summary",
    "supports",
    "scope",
    "task_id",
)
_MAX_CONTEXT_CHARS = 8_000
_MAX_TEXT_VALUE_CHARS = 1_000


def format_verification_evidence_context(
    evidence_items: Sequence[Any],
    *,
    limit: int,
) -> str | None:
    """Return bounded canonical verification evidence for validation."""
    if limit <= 0:
        return None

    blocks: list[str] = []
    for item in evidence_items[-limit:]:
        if not isinstance(item, dict):
            continue
        payload = _structured_evidence_item(item)
        if payload:
            blocks.append(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))

    if not blocks:
        return None

    header = "Structured verification results (one self-contained JSON object per result):\n"
    selected: list[str] = []
    used_chars = len(header)
    for block in reversed(blocks):
        if len(header) + len(block) > _MAX_CONTEXT_CHARS:
            block = json.dumps(
                {
                    "evidence_type": "validation_evidence_overflow",
                    "success": None,
                    "missing_evidence": "A verification result exceeded the evidence budget.",
                },
                separators=(",", ":"),
            )
        if used_chars + len(block) + 1 > _MAX_CONTEXT_CHARS:
            break
        selected.append(block)
        used_chars += len(block) + 1
    return header + "\n".join(reversed(selected))


def _structured_evidence_item(item: dict[str, Any]) -> dict[str, object]:
    command_value = item.get("command")
    command = command_value.strip() if isinstance(command_value, str) else None
    if command == "":
        command = None
    summary = _text(item.get("summary"))
    if command is None and summary is None:
        return {}

    payload: dict[str, object] = {}
    for key in _EVIDENCE_FIELDS:
        value = item.get(key)
        if isinstance(value, str):
            text = value.strip() if key == "command" else _text(value)
            if text is not None:
                payload[key] = text
        elif isinstance(value, bool):
            payload[key] = value
        elif isinstance(value, int):
            payload[key] = value
        elif key == "success" and value is None:
            payload[key] = None

    return payload


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if len(value) > _MAX_TEXT_VALUE_CHARS:
        return value[: _MAX_TEXT_VALUE_CHARS - 3] + "..."
    return value
