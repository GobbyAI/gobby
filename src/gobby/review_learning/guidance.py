"""Compact review-lesson guidance formatting."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_ACTIONABLE_GUIDANCE_FIELDS = ("do", "avoid", "principle", "prevention")


def has_actionable_guidance(lesson: Mapping[str, Any]) -> bool:
    """Return whether a lesson contains non-empty actionable guidance."""
    return any(str(lesson.get(field) or "").strip() for field in _ACTIONABLE_GUIDANCE_FIELDS)


def format_review_lesson_guidance(
    lessons: list[dict[str, Any]],
    *,
    scope_label: str = "matched file",
) -> str:
    """Format matched review lessons for advisory context injection."""
    actionable_lessons = [lesson for lesson in lessons if has_actionable_guidance(lesson)]
    if not actionable_lessons:
        return ""

    lines = ["<review-guidance>"]
    for lesson in actionable_lessons:
        path = lesson.get("matched_file_path") or lesson.get("evidence_path") or scope_label
        pattern_id = lesson.get("pattern_id") or "review-lesson"
        lines.append(f"- {path} [{pattern_id}]")

        do_text = (
            lesson.get("do")
            if "do" in lesson
            else lesson.get("prevention") or lesson.get("principle")
        )
        avoid_text = lesson.get("avoid")
        principle = lesson.get("principle")

        if do_text:
            lines.append(f"  Do: {_trim_terminal(str(do_text))}")
        if avoid_text:
            lines.append(f"  Avoid: {_trim_terminal(str(avoid_text))}")
        if principle and principle != do_text:
            lines.append(f"  Principle: {_trim_terminal(str(principle))}")

    lines.append("</review-guidance>")
    return "\n".join(lines)


def _trim_terminal(value: str) -> str:
    text = " ".join(value.split())
    return text.rstrip(".")
