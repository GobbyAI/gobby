"""Mandatory batch retrieval for daemon-selected memory recalls."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gobby.hooks.memory_recall_delivery import MemoryRecallDeliveryQueue
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.memory.manager import MemoryManager
from gobby.utils.session_context import get_current_session_id


def register_memory_recall_tool(
    registry: InternalToolRegistry,
    memory_manager: MemoryManager,
) -> None:
    """Register the session-scoped batch recall retrieval tool."""
    queue = MemoryRecallDeliveryQueue(memory_manager.db)

    @registry.tool(
        name="get_recall_memories",
        description=(
            "Retrieve every memory selected for one pending recall request in rank order. "
            "Uses the ambient Gobby session and completes the recall gate only after success."
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
            delivery = queue.get(session_id, recall_request_id)
        except Exception as exc:
            return {
                "success": False,
                "recall_request_id": recall_request_id,
                "error": f"Memory retrieval failed: {exc}",
            }
        if delivery is None:
            return {
                "success": False,
                "recall_request_id": recall_request_id,
                "error": "Recall request was not found for the current session.",
            }

        if delivery.get("status") == "pending":
            try:
                pending = queue.pending(session_id)
            except Exception as exc:
                return {
                    "success": False,
                    "recall_request_id": recall_request_id,
                    "error": f"Memory retrieval failed: {exc}",
                }
            if not pending or pending[0].get("recall_request_id") != recall_request_id:
                expected = pending[0].get("recall_request_id") if pending else None
                return {
                    "success": False,
                    "recall_request_id": recall_request_id,
                    "expected_recall_request_id": expected,
                    "error": "Recall requests must be retrieved oldest-first.",
                }

        try:
            memories, missing_memory_ids = _retrieve_memories(memory_manager, delivery)
        except Exception as exc:
            return {
                "success": False,
                "recall_request_id": recall_request_id,
                "error": f"Memory retrieval failed: {exc}",
            }

        if delivery.get("status") == "pending" and not queue.complete(
            session_id,
            delivery,
            delivered_memory_ids=[memory["id"] for memory in memories],
        ):
            return {
                "success": False,
                "recall_request_id": recall_request_id,
                "error": "Recall request changed before it could be completed; retry the call.",
            }

        return {
            "success": True,
            "recall_request_id": recall_request_id,
            "origin_turn_seq": delivery["origin_turn_seq"],
            "memories": memories,
            "missing_memory_ids": missing_memory_ids,
            "total_content_chars": sum(len(memory["content"]) for memory in memories),
        }


def _retrieve_memories(
    memory_manager: MemoryManager,
    delivery: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    references = delivery.get("references")
    if not isinstance(references, list):
        return [], []

    project_id = delivery.get("project_id")
    memories: list[dict[str, Any]] = []
    missing_memory_ids: list[str] = []
    for reference in references:
        if not isinstance(reference, Mapping):
            continue
        memory_id = reference.get("memory_id")
        if not isinstance(memory_id, str) or not memory_id:
            continue
        memory = memory_manager.get_memory(memory_id, project_id=project_id)
        if memory is None:
            missing_memory_ids.append(memory_id)
            continue
        payload = {
            "rank": reference.get("rank"),
            "id": memory_id,
            "content": memory.content,
            "type": memory.memory_type,
            "tags": memory.tags,
        }
        payload.update(
            {key: value for key, value in reference.items() if key not in {"memory_id", "rank"}}
        )
        memories.append(payload)
    return memories, missing_memory_ids
