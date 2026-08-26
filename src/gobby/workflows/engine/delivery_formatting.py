"""Shared formatting for memory-backed MCP results delivered by rules."""

from __future__ import annotations

from typing import Any

from gobby.hooks.events import HookEvent
from gobby.workflows.engine.injection_tracking import InjectionTrackingMixin

_MEMORY_RESULT_FORMATTERS = {
    ("gobby-review-learning", "recall_review_lessons_for_files"): "review_file",
    ("gobby-review-learning", "recall_review_lessons_by_class"): "review_class",
}


def _is_empty_inject_payload(result: Any) -> bool:
    """Return True when a successful MCP payload contains no deliverable items."""
    if not isinstance(result, dict):
        return not result
    for key in ("messages", "memories", "lessons", "results", "items"):
        value = result.get(key)
        if isinstance(value, list):
            return not value
    count = result.get("count")
    return isinstance(count, int) and count == 0


class DeliveryFormattingMixin(InjectionTrackingMixin):
    """Format memory-backed MCP results through one routing registry."""

    def _format_review_lessons_result(
        self,
        result: dict[str, Any],
        platform_session_id: str | None,
        variables: dict[str, Any],
        scope_label: str = "matched file",
    ) -> str | None:
        """Inline pipeline for review lesson results."""
        del variables
        from gobby.review_learning.guidance import format_review_lesson_guidance

        if _is_empty_inject_payload(result):
            return None
        lessons = result.get("lessons") or []
        if not lessons:
            return None
        new_lessons = self._filter_and_track_new_review_lessons(lessons, platform_session_id)
        if not new_lessons:
            return None
        return format_review_lesson_guidance(new_lessons, scope_label=scope_label)

    def _format_memory_backed_result(
        self,
        *,
        server: str,
        tool: str,
        result: dict[str, Any],
        event: HookEvent,
        platform_session_id: str | None,
        variables: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Route review guidance through the formatter registry."""
        del event
        formatter = _MEMORY_RESULT_FORMATTERS.get((server, tool))
        if formatter is None:
            return False, None
        scope_label = "matched lesson class" if formatter == "review_class" else "matched file"
        return True, self._format_review_lessons_result(
            result,
            platform_session_id,
            variables,
            scope_label,
        )
