"""Delivery formatting for workflow effect results."""

import logging
from typing import Any

from gobby.workflows.engine.injection_tracking import InjectionTrackingMixin

logger = logging.getLogger("gobby.workflows.engine.effects")

REVIEW_LESSON_TAG = "review-lesson"


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


def _is_review_lesson_memory(memory: Any) -> bool:
    if not isinstance(memory, dict):
        return False
    tags = memory.get("tags")
    if not isinstance(tags, (list, tuple, set, frozenset)):
        return False
    return REVIEW_LESSON_TAG in tags


class DeliveryFormattingMixin(InjectionTrackingMixin):
    """Format injected MCP results while applying memory delivery policy."""

    def _format_search_memories_result(
        self,
        result: dict[str, Any],
        platform_session_id: str | None,
        variables: dict[str, Any],
        *,
        recall_context: dict[str, Any] | None = None,
    ) -> str | None:
        """Inline pipeline for search_memories results.

        When the outcome recorder is wired and the payload is joinable
        (recall_request_id + platform session), every memory's final
        injected-vs-filtered decision is persisted (contract §5).
        """
        del variables
        from gobby.hooks.dispatchers.mcp import format_project_memories_with_outcome

        if _is_empty_inject_payload(result):
            return None

        memories = result.get("memories") or []
        recall_ctx = recall_context or {
            "recall_request_id": result.get("recall_request_id"),
            "caller": "mcp_proxy.memory.search_memories",
            "project_id": result.get("project_id"),
            "turn_seq": None,
        }
        rows: list[dict[str, Any]] = []
        group_by_id: dict[str, str | None] = {}
        kept: list[Any] = []
        for memory in memories:
            if isinstance(memory, dict):
                memory_id = memory.get("id")
                if isinstance(memory_id, str) and memory_id:
                    memory_type = memory.get("type")
                    group_by_id[memory_id] = memory_type if isinstance(memory_type, str) else None
            if _is_review_lesson_memory(memory):
                self._append_outcome_row(
                    rows,
                    memory,
                    platform_session_id,
                    recall_ctx,
                    outcome="filtered",
                    drop_reason="review_lesson",
                )
                continue
            kept.append(memory)
        if not kept:
            self._record_injection_outcomes(rows)
            return None

        new_memories, dedup_dropped = self._filter_new_memories(kept, platform_session_id)
        for memory in dedup_dropped:
            self._append_outcome_row(
                rows,
                memory,
                platform_session_id,
                recall_ctx,
                outcome="filtered",
                drop_reason="already_injected",
            )
        if not new_memories:
            self._record_injection_outcomes(rows)
            return None

        text, render_outcome = format_project_memories_with_outcome(new_memories)
        for memory_id in render_outcome.empty_content_ids:
            self._append_outcome_row(
                rows,
                memory_id,
                platform_session_id,
                recall_ctx,
                outcome="filtered",
                drop_reason="empty_content",
            )
        for memory_id in render_outcome.omitted_ids:
            self._append_outcome_row(
                rows,
                memory_id,
                platform_session_id,
                recall_ctx,
                outcome="filtered",
                drop_reason="budget",
            )
        for position, memory_id in enumerate(render_outcome.rendered_ids):
            self._append_outcome_row(
                rows,
                memory_id,
                platform_session_id,
                recall_ctx,
                outcome="injected",
                injection_position=position,
                injection_group=group_by_id.get(memory_id),
            )
        self._track_injected_ids(render_outcome.rendered_ids, platform_session_id)
        self._record_injection_outcomes(rows)
        return text or None

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
