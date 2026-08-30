"""Review-lesson injection deduplication for the workflow engine."""

import logging
from typing import Any

logger = logging.getLogger("gobby.workflows.engine.effects")


class InjectionTrackingMixin:
    """Track delivered review-lesson memory IDs."""

    db: Any

    def _filter_and_track_new_review_lessons(
        self,
        lessons: list[Any],
        platform_session_id: str | None,
    ) -> list[dict[str, Any]]:
        """Filter already-injected review lesson memory ids."""
        from gobby.workflows.state_manager import SessionVariableManager

        new_lessons: list[dict[str, Any]] = []
        if not lessons:
            return new_lessons

        from gobby.hooks.receipt_effects import (
            stage_append_set_variables,
            staged_append_set_values,
        )

        sv_mgr = SessionVariableManager(self.db) if platform_session_id else None
        already: set[str] = set()
        if sv_mgr is not None and platform_session_id:
            try:
                existing_vars = sv_mgr.get_variables(platform_session_id)
                already = set(existing_vars.get("injected_review_lesson_ids", []) or [])
            except Exception as exc:  # Tracking failures never block workflow injection.
                logger.debug("Failed to read injected_review_lesson_ids for dedup: %s", exc)
        already |= staged_append_set_values("injected_review_lesson_ids")

        seen: set[str] = set()
        for lesson in lessons:
            if not isinstance(lesson, dict):
                continue
            memory_id = lesson.get("memory_id")
            if not isinstance(memory_id, str) or not memory_id:
                continue
            if memory_id in seen or memory_id in already:
                continue
            seen.add(memory_id)
            new_lessons.append(lesson)

        new_ids = [lesson["memory_id"] for lesson in new_lessons if lesson.get("memory_id")]
        if new_ids and platform_session_id:
            stage_append_set_variables(
                platform_session_id,
                "injected_review_lesson_ids",
                new_ids,
            )

        return new_lessons
