"""Delivery formatting for workflow effect results."""

import logging
from typing import Any

from gobby.workflows.engine.injection_tracking import InjectionTrackingMixin

logger = logging.getLogger("gobby.workflows.engine.effects")


def _is_empty_inject_payload(result: Any) -> bool:
    """Decide whether an mcp_call result represents nothing worth injecting."""
    if not isinstance(result, dict):
        return result is None or not result
    if result.get("count") == 0:
        return True
    bookkeeping = {"success", "count", "response_time_ms", "recall_request_id", "project_id"}
    content_keys = {key for key in result if key not in bookkeeping}
    if content_keys == {"messages"} and not result.get("messages"):
        return True
    if content_keys == {"memories"} and not result.get("memories"):
        return True
    if content_keys <= {"lessons", "message"} and not result.get("lessons"):
        return True
    return False


class DeliveryFormattingMixin(InjectionTrackingMixin):
    """Format injected MCP results while applying memory delivery policy."""

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
