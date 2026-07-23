"""Compact review-lesson guidance formatting."""

from __future__ import annotations

from typing import Any


def format_review_lesson_guidance(
    lessons: list[dict[str, Any]],
    *,
    scope_label: str = "matched file",
) -> str:
    """Format matched review lessons for advisory context injection."""
    if not lessons:
        return ""

    lines = ["<review-guidance>"]
    for lesson in lessons:
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
