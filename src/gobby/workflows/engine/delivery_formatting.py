"""Delivery formatting for workflow effect results."""

import json
import logging
from typing import Any

from gobby.memory.recall_constants import MEMORY_RECALL_PRODUCER
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

    def _format_delivery_result(
        self,
        result: dict[str, Any],
        _platform_session_id: str | None,
        _variables: dict[str, Any],
    ) -> str | None:
        """Inline delivery-time pipeline for deliver_pending_messages results."""
        from gobby.hooks.dispatchers.mcp import format_discovery_result

        if _is_empty_inject_payload(result):
            return None

        messages = result.get("messages") or []
        other_messages: list[Any] = []
        memory_parts: list[str] = []

        for msg in messages:
            if not isinstance(msg, dict):
                other_messages.append(msg)
                continue

            is_memory_message = msg.get("message_type") == "memory_recall"
            content = msg.get("content")
            parsed: Any = None
            if isinstance(content, dict):
                parsed = content
            elif isinstance(content, str):
                try:
                    parsed = json.loads(content)
                except (json.JSONDecodeError, ValueError):
                    parsed = None

            metadata = msg.get("metadata")
            if (
                not (isinstance(parsed, dict) and parsed.get("type") == "memory_recall")
                and isinstance(metadata, dict)
                and metadata.get("type") == "memory_recall"
            ):
                parsed = metadata

            if isinstance(parsed, dict) and parsed.get("type") == "memory_recall":
                formatted_memory = self._format_memory_recall_delivery(
                    parsed,
                    _platform_session_id,
                    _variables,
                )
                if formatted_memory:
                    memory_parts.append(formatted_memory)
            elif is_memory_message:
                logger.debug("Dropping malformed memory_recall delivery payload")
            else:
                other_messages.append(msg)

        parts: list[str] = list(memory_parts)
        if other_messages:
            message_formatted = format_discovery_result(
                {
                    "tool": "deliver_pending_messages",
                    "result": {"messages": other_messages, "count": len(other_messages)},
                },
            )
            if message_formatted:
                parts.append(message_formatted)

        return "\n\n".join(parts) if parts else None

    def _format_memory_recall_delivery(
        self,
        payload: dict[str, Any],
        platform_session_id: str | None,
        variables: dict[str, Any],
    ) -> str | None:
        """Validate and format a deferred daemon memory recall payload.

        Drops and deliveries log at INFO with the recall_request_id so the
        delivery half of the recall funnel is quantifiable from daemon logs
        and joinable to recall signal events (#17772).
        """
        recall_request_id = payload.get("recall_request_id")
        if payload.get("producer") != MEMORY_RECALL_PRODUCER:
            logger.debug("Dropping memory_recall delivery with non-daemon producer")
            return None
        if payload.get("enabled") is False or payload.get("disabled") is True:
            logger.debug("Dropping disabled memory_recall delivery payload")
            return None

        origin_turn_seq = payload.get("origin_turn_seq")
        parent_turn_seq = variables.get("parent_turn_seq")
        valid_origin_seq = isinstance(origin_turn_seq, int) and not isinstance(
            origin_turn_seq, bool
        )
        recall_context = {
            "recall_request_id": recall_request_id,
            "caller": "memory.recall",
            "project_id": payload.get("project_id"),
            "turn_seq": origin_turn_seq if valid_origin_seq else None,
        }
        if (
            not valid_origin_seq
            or not isinstance(parent_turn_seq, int)
            or isinstance(parent_turn_seq, bool)
        ):
            logger.info(
                "Dropping memory_recall delivery without valid turn sequence: "
                "recall_request_id=%s origin=%s parent=%s",
                recall_request_id,
                origin_turn_seq,
                parent_turn_seq,
            )
            self._record_payload_drop(
                payload, platform_session_id, recall_context, "invalid_turn_seq"
            )
            return None
        if origin_turn_seq != parent_turn_seq - 1:
            logger.info(
                "Dropping stale memory_recall delivery: recall_request_id=%s "
                "origin=%s parent=%s reason=delivery_turn_seq_mismatch",
                recall_request_id,
                origin_turn_seq,
                parent_turn_seq,
            )
            self._record_payload_drop(
                payload, platform_session_id, recall_context, "stale_delivery"
            )
            return None

        memories = payload.get("memories")
        if not isinstance(memories, list):
            logger.info(
                "Dropping memory_recall delivery with malformed memories: recall_request_id=%s",
                recall_request_id,
            )
            return None
        formatted = self._format_search_memories_result(
            {"memories": memories},
            platform_session_id,
            variables,
            recall_context=recall_context,
        )
        if formatted is None:
            logger.info(
                "Dropping memory_recall delivery emptied by review-lesson filter or "
                "delivery dedup: recall_request_id=%s memories=%d reason=delivery_dedup",
                recall_request_id,
                len(memories),
            )
            return None
        logger.info(
            "Delivered memory_recall injection: recall_request_id=%s payload_memories=%d "
            "origin_turn_seq=%s",
            recall_request_id,
            len(memories),
            origin_turn_seq,
        )
        return formatted

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
