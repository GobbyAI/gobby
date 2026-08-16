"""Shared formatting and staged delivery for memory-backed MCP results."""

from __future__ import annotations

import logging
from typing import Any

from gobby.hooks.context_limits import (
    additional_context_limit_for,
    inline_context_budget_for,
)
from gobby.hooks.events import HookEvent, HookResponse
from gobby.hooks.memory_recall_delivery import MemoryRecallDeliveryQueue
from gobby.memory.context import format_memory_metadata_suffix
from gobby.workflows.engine.injection_tracking import InjectionTrackingMixin
from gobby.workflows.state_manager import SessionVariableManager

_STAGED_MEMORY_RECALLS = "_staged_memory_recalls"
_MEMORY_RESULT_FORMATTERS = {
    ("gobby-review-learning", "recall_review_lessons_for_files"): "review_file",
    ("gobby-review-learning", "recall_review_lessons_by_class"): "review_class",
    ("gobby-memory", "recall_memories_for_prompt"): "project_memory",
}
_MEMORY_TYPE_HEADINGS = {
    "context": "Project Context",
    "preference": "Preferences",
    "pattern": "Patterns",
    "fact": "Facts",
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
        """Route review guidance and generic memories through one registry."""
        formatter = _MEMORY_RESULT_FORMATTERS.get((server, tool))
        if formatter is None:
            return False, None
        if formatter in {"review_file", "review_class"}:
            scope_label = "matched lesson class" if formatter == "review_class" else "matched file"
            return True, self._format_review_lessons_result(
                result,
                platform_session_id,
                variables,
                scope_label,
            )

        memories = result.get("memories")
        if not isinstance(memories, list) or not memories:
            return True, None
        staged = event.metadata.setdefault(_STAGED_MEMORY_RECALLS, [])
        if isinstance(staged, list):
            staged.append(result)
        return True, None


def finalize_staged_memory_delivery(
    event: HookEvent,
    response: HookResponse,
    *,
    database: Any,
    logger: logging.Logger,
) -> None:
    """Commit staged generic memories after complete hook context is known."""
    staged = event.metadata.pop(_STAGED_MEMORY_RECALLS, None)
    if not isinstance(staged, list) or not staged:
        return
    raw_session_id = event.metadata.get("_platform_session_id")
    session_id = raw_session_id if isinstance(raw_session_id, str) and raw_session_id else None
    if session_id is None or database is None:
        return

    budget = inline_context_budget_for(event.source)
    ship_limit = additional_context_limit_for(event.source)
    queue = MemoryRecallDeliveryQueue(database)
    injected_ids: list[str] = []

    for delivery in staged:
        if not isinstance(delivery, dict):
            continue
        raw_memories = delivery.get("memories")
        if not isinstance(raw_memories, list):
            continue
        memories = [memory for memory in raw_memories if _valid_memory_body(memory)]
        if not memories:
            continue
        bodies = [_format_project_memory(memory) for memory in memories]
        recall_request_id = delivery.get("recall_request_id")
        origin_turn_seq = delivery.get("origin_turn_seq")
        if not isinstance(recall_request_id, str) or not isinstance(origin_turn_seq, int):
            continue

        complete_context = _joined_context(response.context, bodies)
        if len(complete_context) <= budget:
            response.context = complete_context
            injected_ids.extend(memory["id"] for memory in memories)
            continue

        instruction = (
            'call_tool("gobby-memory", "get_recall_memories", '
            f'{{"recall_request_id":"{recall_request_id}"}})'
        )
        inline_count = _largest_fitting_prefix(
            response.context,
            bodies,
            instruction,
            budget,
        )
        inline_memories = memories[:inline_count]
        overflow_memories = memories[inline_count:]
        if queue.queue(
            session_id,
            recall_request_id=recall_request_id,
            origin_turn_seq=origin_turn_seq,
            project_id=(
                delivery.get("project_id") if isinstance(delivery.get("project_id"), str) else None
            ),
            memories=overflow_memories,
        ):
            candidate = _joined_context(
                response.context,
                [*bodies[:inline_count], instruction],
            )
            if len(candidate) <= ship_limit:
                response.context = candidate
            else:
                instruction_only = _joined_context(response.context, [instruction])
                if len(instruction_only) <= ship_limit:
                    response.context = instruction_only
            injected_ids.extend(memory["id"] for memory in inline_memories)
        else:
            logger.warning(
                "Memory recall overflow queue failed; delivering full context inline: request=%s",
                recall_request_id,
            )
            response.context = complete_context
            injected_ids.extend(memory["id"] for memory in memories)

    if injected_ids:
        SessionVariableManager(database).append_to_set_variable(
            session_id,
            "injected_memory_ids",
            injected_ids,
        )


def _largest_fitting_prefix(
    existing: str | None,
    bodies: list[str],
    instruction: str,
    budget: int,
) -> int:
    for count in range(len(bodies) - 1, -1, -1):
        if len(_joined_context(existing, [*bodies[:count], instruction])) <= budget:
            return count
    return 0


def _joined_context(existing: str | None, additions: list[str]) -> str:
    parts = [part for part in [existing, *additions] if part]
    return "\n\n".join(parts)


def _valid_memory_body(memory: Any) -> bool:
    return (
        isinstance(memory, dict)
        and isinstance(memory.get("id"), str)
        and bool(memory.get("id"))
        and isinstance(memory.get("content"), str)
    )


def _format_project_memory(memory: dict[str, Any]) -> str:
    memory_id = memory["id"]
    content = memory["content"]
    memory_type = memory.get("memory_type")
    canonical_type = memory_type if isinstance(memory_type, str) else "fact"
    heading = _MEMORY_TYPE_HEADINGS.get(canonical_type, "Facts")
    body = (
        content.strip()
        if canonical_type == "context"
        else f"- {content.strip().lstrip('-*•').strip()}"
    )
    return "\n".join(
        [
            "<project-memory>",
            f"## {heading}",
            f"{body}{format_memory_metadata_suffix(memory_id)}",
            "</project-memory>",
        ]
    )
