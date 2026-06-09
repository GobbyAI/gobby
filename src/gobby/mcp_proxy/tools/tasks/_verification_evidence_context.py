"""Formatting helpers for close-task verification evidence context."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

_EVIDENCE_FIELDS: tuple[tuple[str, str], ...] = (
    ("command", "command"),
    ("matcher_id", "matcher_id"),
    ("matcher_label", "matcher_label"),
    ("summary", "summary"),
    ("supports", "supports"),
    ("scope", "scope"),
    ("task_id", "task_id"),
)


def format_verification_evidence_context(
    evidence_items: Sequence[Any],
    *,
    limit: int,
) -> str | None:
    """Return bounded successful verification evidence for LLM validation context."""
    if limit <= 0:
        return None

    blocks: list[str] = []
    for item in evidence_items[-limit:]:
        if not isinstance(item, dict) or item.get("success") is not True:
            continue
        lines = _format_evidence_item(item)
        if lines:
            blocks.append("\n".join(lines))

    if not blocks:
        return None

    return "Successful verification evidence:\n" + "\n".join(blocks)


def _format_evidence_item(item: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, label in _EVIDENCE_FIELDS:
        value = _text(item.get(key))
        if value is None:
            continue
        prefix = "- " if not lines else "  "
        lines.append(f"{prefix}{label}: {value}")

    if not _text(item.get("command")) and not _text(item.get("summary")):
        return []
    return lines


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None
