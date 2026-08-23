"""Inline memory recall and deterministic overflow retrieval tools."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.config.sessions import MemoryRecallConfig
from gobby.hooks.events import HookEvent, HookEventType, parse_session_source
from gobby.hooks.memory_recall_delivery import MemoryRecallDeliveryQueue
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.memory.recall import MemoryRecallRunner
from gobby.utils.project_context import get_project_context
from gobby.utils.session_context import get_current_session_id

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from gobby.memory.manager import MemoryManager

MAX_DIRECT_MCP_SERIALIZED_CHARS = 11_900


def register_memory_recall_tool(
    registry: InternalToolRegistry,
    memory_manager_resolver: Callable[[], MemoryManager | None],
    *,
    config_resolver: Callable[[], MemoryRecallConfig | None] | None = None,
) -> None:
    """Register inline recall and overflow-only retrieval."""

    def _current_queue() -> MemoryRecallDeliveryQueue | None:
        manager = memory_manager_resolver()
        return MemoryRecallDeliveryQueue(manager.db) if manager is not None else None

    def _current_runner() -> MemoryRecallRunner | None:
        manager = memory_manager_resolver()
        if manager is None:
            return None
        recall_config = config_resolver() if config_resolver is not None else None
        return MemoryRecallRunner(
            db=manager.db,
            memory_manager=manager,
            config=recall_config or MemoryRecallConfig(),
        )

    @registry.tool(
        name="recall_memories_for_prompt",
        description=(
            "Return up to three direct hybrid-search memory results for one "
            "parent-user prompt, for inline hook delivery."
        ),
    )
    async def recall_memories_for_prompt(
        prompt: str,
        source: str,
        parent_turn_seq: str,
        is_spawned_agent: bool = False,
    ) -> dict[str, Any]:
        session_id = get_current_session_id()
        if not session_id:
            return {"success": False, "error": "No ambient Gobby session is available."}
        try:
            normalized_parent_turn_seq = int(parent_turn_seq)
        except ValueError:
            return {"success": False, "error": "parent_turn_seq must be an integer."}

        project_context = get_project_context() or {}
        project_id = project_context.get("id")
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=session_id,
            source=parse_session_source(source),
            timestamp=datetime.now(UTC),
            data={"prompt": prompt},
            project_id=project_id if isinstance(project_id, str) else None,
            metadata={"_platform_session_id": session_id},
        )
        variables = {
            "parent_turn_seq": normalized_parent_turn_seq,
            "is_spawned_agent": is_spawned_agent,
        }
        try:
            runner = _current_runner()
        except RuntimeError:
            return {"success": False, "error": "Memory services are unavailable."}
        if runner is None:
            return {"success": False, "error": "Memory services are unavailable."}
        result = await runner.run(event, session_id, variables)
        if result is None:
            return {"success": True, "skipped": True, "memories": []}
        return {
            "success": True,
            "recall_request_id": result.recall_request_id,
            "origin_turn_seq": result.origin_turn_seq,
            "project_id": event.project_id,
            "memories": result.memories,
        }

    @registry.tool(
        name="get_recall_memories",
        description=(
            "Retrieve the next deterministic chunk for the oldest pending memory recall "
            "overflow. Completion and the injected-ID ledger update occur on the final chunk."
        ),
    )
    def get_recall_memories(recall_request_id: str) -> dict[str, Any]:
        session_id = get_current_session_id()
        if not session_id:
            return {
                "success": False,
                "recall_request_id": recall_request_id,
                "error": "No ambient Gobby session is available.",
            }
        try:
            queue = _current_queue()
        except RuntimeError:
            logger.exception(
                "Failed to resolve the memory recall delivery queue",
                extra={
                    "recall_request_id": recall_request_id,
                    "session_id": session_id,
                },
            )
            return _retrieval_error(recall_request_id, "Memory retrieval failed.")
        if queue is None:
            return _retrieval_error(recall_request_id, "Memory services are unavailable.")
        try:
            delivery = queue.get(session_id, recall_request_id)
            pending = queue.pending(session_id)
        except Exception as exc:
            return _retrieval_error(recall_request_id, f"Memory retrieval failed: {exc}")
        if delivery is None:
            return _retrieval_error(
                recall_request_id,
                "Recall request was not found for the current session.",
            )
        if delivery.get("status") != "pending":
            return _retrieval_error(recall_request_id, "Recall request is already complete.")
        if not pending or pending[0].get("recall_request_id") != recall_request_id:
            expected = pending[0].get("recall_request_id") if pending else None
            return {
                **_retrieval_error(
                    recall_request_id,
                    "Recall requests must be retrieved oldest-first.",
                ),
                "expected_recall_request_id": expected,
            }

        try:
            payload, next_cursor = _next_chunk(delivery)
        except (TypeError, ValueError) as exc:
            return _retrieval_error(recall_request_id, f"Memory retrieval failed: {exc}")
        if not queue.advance(
            session_id,
            delivery,
            next_cursor=next_cursor,
            final_chunk=payload["final_chunk"],
        ):
            return _retrieval_error(
                recall_request_id,
                "Recall request changed before the chunk was committed; retry the call.",
            )
        return payload


def _next_chunk(
    delivery: Mapping[str, Any],
    *,
    max_serialized_chars: int = MAX_DIRECT_MCP_SERIALIZED_CHARS,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Pack whole memories into one chunk, never slicing a body to fit the budget.

    A chunk takes memories in queued order while the serialized payload stays
    under the budget, and always carries at least one memory. That last rule is
    deliberate fail-open: an over-budget response is recoverable by the caller,
    while a body cut in half reaches the model as a memory that says something
    its author never wrote.
    """
    memories = delivery.get("memories")
    cursor = delivery.get("cursor")
    if not isinstance(memories, list) or not isinstance(cursor, Mapping):
        raise TypeError("invalid queued delivery")
    memory_index = cursor.get("memory_index")
    chunk_index = cursor.get("chunk_index")
    if not all(isinstance(value, int) and value >= 0 for value in cursor.values()):
        raise TypeError("invalid queued cursor")
    if not isinstance(memory_index, int) or not isinstance(chunk_index, int):
        raise TypeError("invalid queued cursor")
    if memory_index >= len(memories):
        raise ValueError("queued cursor is past the final memory")

    def build(bodies: list[dict[str, Any]], next_memory_index: int) -> dict[str, Any]:
        return {
            "success": True,
            "recall_request_id": delivery["recall_request_id"],
            "chunk_index": chunk_index,
            "final_chunk": next_memory_index == len(memories),
            "memories": bodies,
        }

    packed: list[dict[str, Any]] = []
    payload = build(packed, memory_index)
    next_memory_index = memory_index
    while next_memory_index < len(memories):
        candidate = [*packed, _memory_payload(memories[next_memory_index])]
        candidate_payload = build(candidate, next_memory_index + 1)
        if packed and _serialized_chars(candidate_payload) >= max_serialized_chars:
            break
        packed = candidate
        payload = candidate_payload
        next_memory_index += 1

    return payload, {"memory_index": next_memory_index, "chunk_index": chunk_index + 1}


def _memory_payload(memory: Any) -> dict[str, Any]:
    if not isinstance(memory, Mapping):
        raise TypeError("invalid queued memory")
    memory_id = memory.get("id")
    content = memory.get("content")
    memory_type = memory.get("memory_type")
    if not isinstance(memory_id, str) or not isinstance(content, str):
        raise TypeError("invalid queued memory body")
    return {
        "id": memory_id,
        "memory_type": memory_type if isinstance(memory_type, str) else "fact",
        "content": content,
    }


def _serialized_chars(payload: Mapping[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _retrieval_error(recall_request_id: str, error: str) -> dict[str, Any]:
    return {
        "success": False,
        "recall_request_id": recall_request_id,
        "error": error,
    }
