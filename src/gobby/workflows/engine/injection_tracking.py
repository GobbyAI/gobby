"""Memory-index and review-lesson injection deduplication for the workflow engine."""

import logging
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger("gobby.workflows.engine.effects")

# ``"<memory_id>@<seq>"`` stamps, one per rendering; the latest seq per id counts.
SURFACED_MEMORY_IDS_VARIABLE = "surfaced_memory_ids"
# ``{memory_id, task_id}`` records written by ``gobby-memory:get_memory``.
ACCESSED_MEMORY_IDS_VARIABLE = "accessed_memory_ids"
MEMORY_SURFACE_SEQ_VARIABLE = "_memory_surface_seq"
DEFAULT_RESHOW_AFTER_INJECTIONS = 5


def _latest_stamps(values: Iterable[Any]) -> dict[str, int]:
    latest: dict[str, int] = {}
    for value in values:
        if not isinstance(value, str):
            continue
        memory_id, _, seq = value.rpartition("@")
        if memory_id and seq.isdigit():
            latest[memory_id] = max(latest.get(memory_id, 0), int(seq))
    return latest


class InjectionTrackingMixin:
    """Track delivered memory-index and review-lesson memory IDs."""

    db: Any

    def _filter_and_track_new_memories(
        self,
        memories: list[Any],
        platform_session_id: str | None,
        reshow_after_injections: int = DEFAULT_RESHOW_AFTER_INJECTIONS,
    ) -> list[dict[str, Any]]:
        """Drop fetched ids and ids shown within the horizon; stamp what renders.

        Every surfacing advances ``_memory_surface_seq`` whether or not a line
        renders. A shown-but-unread id returns once ``reshow_after_injections``
        further surfacings have passed. Stamps and the sequence are staged, so a
        payload that never reached the agent suppresses nothing.
        """
        from gobby.workflows.state_manager import SessionVariableManager

        new_memories: list[dict[str, Any]] = []

        from gobby.hooks.receipt_effects import (
            record_worker_staging,
            stage_append_set_variables,
            staged_append_set_values,
            staged_session_variable,
        )

        committed: dict[str, Any] = {}
        if platform_session_id:
            try:
                committed = SessionVariableManager(self.db).get_variable_subset(
                    platform_session_id,
                    (
                        MEMORY_SURFACE_SEQ_VARIABLE,
                        ACCESSED_MEMORY_IDS_VARIABLE,
                        SURFACED_MEMORY_IDS_VARIABLE,
                    ),
                )
            except Exception as exc:  # Tracking failures never block workflow injection.
                logger.debug("Failed to read memory tracking variables for dedup: %s", exc)
        staged_seq = staged_session_variable(MEMORY_SURFACE_SEQ_VARIABLE)
        last_seq = (
            staged_seq if staged_seq is not None else committed.get(MEMORY_SURFACE_SEQ_VARIABLE)
        )
        seq_now = (last_seq if isinstance(last_seq, int) else 0) + 1
        accessed = {
            record.get("memory_id")
            for record in committed.get(ACCESSED_MEMORY_IDS_VARIABLE) or []
            if isinstance(record, dict)
        }
        stamps = _latest_stamps(
            [
                *(committed.get(SURFACED_MEMORY_IDS_VARIABLE) or []),
                *staged_append_set_values(SURFACED_MEMORY_IDS_VARIABLE),
            ]
        )

        seen: set[str] = set()
        for memory in memories:
            if not isinstance(memory, dict):
                continue
            memory_id = memory.get("id")
            if not isinstance(memory_id, str) or not memory_id:
                continue
            if memory_id in seen or memory_id in accessed:
                continue
            seen.add(memory_id)
            stamped = stamps.get(memory_id)
            if stamped is not None and seq_now - stamped < reshow_after_injections:
                continue
            new_memories.append(memory)

        if platform_session_id:
            stage_append_set_variables(
                platform_session_id,
                SURFACED_MEMORY_IDS_VARIABLE,
                [f"{memory['id']}@{seq_now}" for memory in new_memories],
            )
            record_worker_staging(
                {
                    "session_id": platform_session_id,
                    "session_variables": {MEMORY_SURFACE_SEQ_VARIABLE: seq_now},
                }
            )

        return new_memories

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
                existing_vars = sv_mgr.get_variable_subset(
                    platform_session_id, ("injected_review_lesson_ids",)
                )
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
