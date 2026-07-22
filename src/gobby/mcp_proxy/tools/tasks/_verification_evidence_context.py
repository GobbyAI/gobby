"""Formatting helpers for close-task verification evidence context."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from gobby.workflows.verification_evidence import correlate_validation_command_result

_EVIDENCE_FIELDS: tuple[str, ...] = (
    "evidence_type",
    "command",
    "exit_code",
    "success",
    "matcher_id",
    "matcher_label",
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
    """Return bounded, command-correlated verification evidence for validation."""
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
    command = _text(item.get("command"))
    summary = _text(item.get("summary"))
    if command is None and summary is None:
        return {}

    payload: dict[str, object] = {}
    for key in _EVIDENCE_FIELDS:
        value = item.get(key)
        if isinstance(value, str):
            text = _text(value)
            if text is not None:
                payload[key] = text
        elif isinstance(value, bool):
            payload[key] = value
        elif isinstance(value, int):
            payload[key] = value

    if command is not None:
        correlated = correlate_validation_command_result(item)
        payload["command_result_correlation"] = "correlated" if correlated else "missing"
        if correlated is not None:
            payload["success"] = correlated.success
            payload["command_result_signal"] = correlated.signal
        else:
            payload["success"] = None
            payload["missing_evidence"] = (
                "trusted terminal provider status or consistent integer exit_code correlated "
                f"to command: {command}"
            )
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
