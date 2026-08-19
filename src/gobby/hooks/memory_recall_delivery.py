"""Durable overflow queue for inline memory recall delivery."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from gobby.storage.hub.protocol import HubDatabase, SessionVariableMutation
from gobby.workflows.state_manager import (
    SessionVariableManager,
    _decode_variables_payload,
    _encode_variables_payload,
)

MEMORY_RECALL_DELIVERIES_VARIABLE = "memory_recall_deliveries"
MAX_MEMORY_RECALL_DELIVERIES = 16
_PENDING = "pending"
_COMPLETE = "complete"


class MemoryRecallDeliveryQueue:
    """Persist only memory bodies that overflow a hook-context budget."""

    def __init__(self, database: HubDatabase) -> None:
        self._database = database
        self._variables = SessionVariableManager(database)

    def queue(
        self,
        session_id: str,
        *,
        recall_request_id: str,
        origin_turn_seq: int,
        project_id: str | None,
        memories: Sequence[Mapping[str, Any]],
    ) -> bool:
        """Upsert one pending overflow delivery for a parent turn."""
        bodies = _memory_bodies(memories)
        if not bodies:
            return False
        delivery = {
            "recall_request_id": recall_request_id,
            "origin_turn_seq": origin_turn_seq,
            "project_id": project_id,
            "status": _PENDING,
            "memories": bodies,
            "cursor": {
                "memory_index": 0,
                "content_offset": 0,
                "chunk_index": 0,
            },
        }
        self._variables.upsert_bounded_list_variable(
            session_id,
            MEMORY_RECALL_DELIVERIES_VARIABLE,
            delivery,
            identity={"origin_turn_seq": origin_turn_seq},
            max_items=MAX_MEMORY_RECALL_DELIVERIES,
        )
        return True

    def pending(self, session_id: str) -> list[dict[str, Any]]:
        """Return valid pending deliveries oldest-first."""
        return [
            dict(delivery)
            for delivery in self._stored(session_id)
            if isinstance(delivery, Mapping)
            and delivery.get("status") == _PENDING
            and _valid_delivery(delivery)
        ]

    def get(self, session_id: str, recall_request_id: str) -> dict[str, Any] | None:
        """Return one valid delivery owned by the current session."""
        for delivery in self._stored(session_id):
            if (
                isinstance(delivery, Mapping)
                and delivery.get("recall_request_id") == recall_request_id
                and delivery.get("status") in {_PENDING, _COMPLETE}
                and _valid_delivery(delivery)
            ):
                return dict(delivery)
        return None

    def advance(
        self,
        session_id: str,
        delivery: Mapping[str, Any],
        *,
        next_cursor: Mapping[str, int],
        final_chunk: bool,
    ) -> bool:
        """Advance one unchanged cursor and complete only after its final chunk."""
        if not _valid_delivery(delivery):
            return False
        expected_cursor = _cursor(delivery)
        if expected_cursor is None or not _valid_cursor(next_cursor):
            return False

        now = datetime.now(UTC).isoformat()
        with self._database.transaction_immediate(
            SessionVariableMutation(session_id=session_id)
        ) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            if not row:
                return False
            variables = _decode_variables_payload(row["variables"])
            stored = variables.get(MEMORY_RECALL_DELIVERIES_VARIABLE, [])
            if not isinstance(stored, list):
                return False

            matched = False
            updated: list[Any] = []
            for stored_delivery in stored:
                if not isinstance(stored_delivery, Mapping):
                    updated.append(stored_delivery)
                    continue
                if not _same_delivery(stored_delivery, delivery):
                    updated.append(dict(stored_delivery))
                    continue
                matched = True
                if stored_delivery.get("status") != _PENDING:
                    return False
                if _cursor(stored_delivery) != expected_cursor:
                    return False
                changed = dict(stored_delivery)
                changed["cursor"] = dict(next_cursor)
                if final_chunk:
                    changed["status"] = _COMPLETE
                    changed["completed_at"] = now
                updated.append(changed)

            if not matched:
                return False
            variables[MEMORY_RECALL_DELIVERIES_VARIABLE] = updated
            if final_chunk:
                existing = variables.get("injected_memory_ids", [])
                existing_ids = (
                    {value for value in existing if isinstance(value, str) and value}
                    if isinstance(existing, list)
                    else set()
                )
                variables["injected_memory_ids"] = sorted(
                    existing_ids | set(_delivery_memory_ids(delivery))
                )
            conn.execute(
                "UPDATE session_variables SET variables = %s, updated_at = %s "
                "WHERE session_id = %s",
                (_encode_variables_payload(variables), now, session_id),
            )
            return True

    def _stored(self, session_id: str) -> list[Any]:
        stored = self._variables.get_variables(session_id).get(
            MEMORY_RECALL_DELIVERIES_VARIABLE,
            [],
        )
        return stored if isinstance(stored, list) else []


def _memory_bodies(memories: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for memory in memories:
        memory_id = memory.get("id")
        content = memory.get("content")
        if (
            not isinstance(memory_id, str)
            or not memory_id
            or memory_id in seen
            or not isinstance(content, str)
        ):
            continue
        seen.add(memory_id)
        memory_type = memory.get("memory_type")
        body: dict[str, Any] = {
            "id": memory_id,
            "content": content,
            "memory_type": memory_type if isinstance(memory_type, str) else "fact",
        }
        rationale = memory.get("rationale")
        if isinstance(rationale, str) and rationale:
            body["rationale"] = rationale
        bodies.append(body)
    return bodies


def _valid_delivery(delivery: Mapping[str, Any]) -> bool:
    return (
        isinstance(delivery.get("recall_request_id"), str)
        and bool(delivery.get("recall_request_id"))
        and isinstance(delivery.get("origin_turn_seq"), int)
        and bool(_delivery_memory_ids(delivery))
        and _cursor(delivery) is not None
    )


def _same_delivery(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return left.get("recall_request_id") == right.get("recall_request_id") and left.get(
        "origin_turn_seq"
    ) == right.get("origin_turn_seq")


def _cursor(delivery: Mapping[str, Any]) -> dict[str, int] | None:
    value = delivery.get("cursor")
    if not isinstance(value, Mapping) or not _valid_cursor(value):
        return None
    return {
        "memory_index": value["memory_index"],
        "content_offset": value["content_offset"],
        "chunk_index": value["chunk_index"],
    }


def _valid_cursor(value: Mapping[str, Any]) -> bool:
    return all(
        isinstance(value.get(key), int) and value[key] >= 0
        for key in ("memory_index", "content_offset", "chunk_index")
    )


def _delivery_memory_ids(delivery: Mapping[str, Any]) -> list[str]:
    memories = delivery.get("memories")
    if not isinstance(memories, list):
        return []
    return [
        memory["id"]
        for memory in memories
        if isinstance(memory, Mapping)
        and isinstance(memory.get("id"), str)
        and memory.get("id")
        and isinstance(memory.get("content"), str)
    ]
