"""Memory injection deduplication and outcome bookkeeping for the workflow engine."""

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("gobby.workflows.engine.effects")


class InjectionTrackingMixin:
    """Track delivered memory IDs and persist injection outcomes."""

    db: Any
    _injection_outcome_recorder: Callable[[list[dict[str, Any]]], None] | None

    def _filter_new_memories(
        self,
        memories: list[Any],
        platform_session_id: str | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split memories into not-yet-injected and dedup-dropped lists.

        Read-only against the ``injected_memory_ids`` session variable;
        rendered ids are appended separately via ``_track_injected_ids`` so
        only memories that actually reach the rendered block count as injected.
        """
        from gobby.workflows.state_manager import SessionVariableManager

        new_memories: list[dict[str, Any]] = []
        dedup_dropped: list[dict[str, Any]] = []
        if not memories:
            return new_memories, dedup_dropped

        already: set[str] = set()
        if platform_session_id:
            try:
                sv_mgr = SessionVariableManager(self.db)
                existing_vars = sv_mgr.get_variables(platform_session_id)
                already = set(existing_vars.get("injected_memory_ids", []) or [])
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to read injected_memory_ids for dedup: %s", exc)

        seen: set[str] = set()
        for memory in memories:
            if not isinstance(memory, dict):
                continue
            memory_id = memory.get("id")
            if not isinstance(memory_id, str) or not memory_id:
                continue
            if memory_id in seen:
                continue
            seen.add(memory_id)
            if memory_id in already:
                dedup_dropped.append(memory)
                continue
            new_memories.append(memory)

        return new_memories, dedup_dropped

    def _track_injected_ids(
        self,
        memory_ids: list[str],
        platform_session_id: str | None,
    ) -> None:
        """Append rendered memory ids to the ``injected_memory_ids`` session variable."""
        if not memory_ids or not platform_session_id:
            return
        from gobby.workflows.state_manager import SessionVariableManager

        try:
            sv_mgr = SessionVariableManager(self.db)
            sv_mgr.append_to_set_variable(platform_session_id, "injected_memory_ids", memory_ids)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to append injected_memory_ids: %s", exc)

    def _append_outcome_row(
        self,
        rows: list[dict[str, Any]],
        memory: Any,
        platform_session_id: str | None,
        recall_context: dict[str, Any],
        *,
        outcome: str,
        drop_reason: str | None = None,
        drop_detail: str | None = None,
        injection_position: int | None = None,
        injection_group: str | None = None,
    ) -> None:
        """Append one contract-§5 outcome row when the payload is joinable."""
        if getattr(self, "_injection_outcome_recorder", None) is None:
            return
        if not platform_session_id or not recall_context.get("recall_request_id"):
            return
        memory_id = memory.get("id") if isinstance(memory, dict) else memory
        if not isinstance(memory_id, str) or not memory_id:
            return
        rows.append(
            {
                "session_id": platform_session_id,
                "recall_request_id": recall_context["recall_request_id"],
                "memory_id": memory_id,
                "project_id": recall_context.get("project_id"),
                "outcome": outcome,
                "drop_reason": drop_reason,
                "drop_detail": drop_detail,
                "injection_position": injection_position,
                "injection_group": injection_group,
                "turn_seq": recall_context.get("turn_seq"),
                "caller": recall_context.get("caller") or "memory.recall",
            }
        )

    def _record_injection_outcomes(self, rows: list[dict[str, Any]]) -> None:
        """Persist collected outcome rows through the fail-open recorder."""
        recorder = getattr(self, "_injection_outcome_recorder", None)
        if recorder is None or not rows:
            return
        try:
            recorder(rows)
        except Exception:  # noqa: BLE001
            logger.debug("Failed to record injection outcomes", exc_info=True)

    def _record_payload_drop(
        self,
        payload: dict[str, Any],
        platform_session_id: str | None,
        recall_context: dict[str, Any],
        drop_detail: str,
    ) -> None:
        """Record a whole-payload delivery drop as filtered rows for each memory."""
        memories = payload.get("memories")
        if not isinstance(memories, list):
            return
        rows: list[dict[str, Any]] = []
        for memory in memories:
            self._append_outcome_row(
                rows,
                memory,
                platform_session_id,
                recall_context,
                outcome="filtered",
                drop_reason="other",
                drop_detail=drop_detail,
            )
        self._record_injection_outcomes(rows)

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

        sv_mgr = SessionVariableManager(self.db) if platform_session_id else None
        already: set[str] = set()
        if sv_mgr is not None and platform_session_id:
            try:
                existing_vars = sv_mgr.get_variables(platform_session_id)
                already = set(existing_vars.get("injected_review_lesson_ids", []) or [])
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to read injected_review_lesson_ids for dedup: %s", exc)

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
        if new_ids and sv_mgr is not None and platform_session_id:
            try:
                sv_mgr.append_to_set_variable(
                    platform_session_id,
                    "injected_review_lesson_ids",
                    new_ids,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("Failed to append injected_review_lesson_ids: %s", exc)

        return new_lessons
