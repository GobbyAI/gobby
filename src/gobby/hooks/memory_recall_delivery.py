"""Durable queue state for self-directed memory recall retrieval."""

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
_REFERENCE_METADATA_FIELDS = (
    "similarity",
    "search_via",
    "ranking_score",
    "raw_semantic_score",
    "temporal_decay_factor",
    "ranking_mode",
)


class MemoryRecallDeliveryQueue:
    """Persist and complete ranked memory recall requests for one session."""

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
        """Upsert a pending delivery for a parent turn."""
        references = _memory_references(memories)
        if not references:
            return False
        delivery = {
            "recall_request_id": recall_request_id,
            "origin_turn_seq": origin_turn_seq,
            "project_id": project_id,
            "status": _PENDING,
            "references": references,
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
        """Return valid pending deliveries in persisted order."""
        stored = self._stored(session_id)
        return [
            dict(delivery)
            for delivery in stored
            if isinstance(delivery, Mapping)
            and delivery.get("status") == _PENDING
            and _valid_delivery_identity(delivery)
            and _delivery_memory_ids(delivery)
        ]

    def get(self, session_id: str, recall_request_id: str) -> dict[str, Any] | None:
        """Return a valid pending or completed delivery for this session."""
        for delivery in self._stored(session_id):
            if (
                isinstance(delivery, Mapping)
                and delivery.get("recall_request_id") == recall_request_id
                and delivery.get("status") in {_PENDING, _COMPLETE}
                and _valid_delivery_identity(delivery)
                and _delivery_memory_ids(delivery)
            ):
                return dict(delivery)
        return None

    def complete(
        self,
        session_id: str,
        delivery: Mapping[str, Any],
        *,
        delivered_memory_ids: Sequence[str],
    ) -> bool:
        """Atomically complete one unchanged delivery and track returned memory IDs."""
        if not _valid_delivery_identity(delivery):
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

            expected_request_id = delivery["recall_request_id"]
            expected_origin_turn_seq = delivery["origin_turn_seq"]
            allowed_memory_ids = set(_delivery_memory_ids(delivery))
            returned_memory_ids = {
                memory_id
                for memory_id in delivered_memory_ids
                if isinstance(memory_id, str) and memory_id in allowed_memory_ids
            }
            matched = False
            changed = False
            updated: list[Any] = []
            for stored_delivery in stored:
                if not isinstance(stored_delivery, Mapping):
                    updated.append(stored_delivery)
                    continue
                if (
                    stored_delivery.get("origin_turn_seq") != expected_origin_turn_seq
                    or stored_delivery.get("recall_request_id") != expected_request_id
                ):
                    updated.append(dict(stored_delivery))
                    continue
                matched = True
                if stored_delivery.get("status") == _COMPLETE:
                    updated.append(dict(stored_delivery))
                    continue
                if stored_delivery.get("status") != _PENDING:
                    return False
                completed = dict(stored_delivery)
                completed["status"] = _COMPLETE
                completed["completed_at"] = now
                updated.append(completed)
                changed = True

            if not matched:
                return False
            if not changed:
                return True
            variables[MEMORY_RECALL_DELIVERIES_VARIABLE] = updated
            injected = variables.get("injected_memory_ids", [])
            existing_ids = (
                {value for value in injected if isinstance(value, str) and value}
                if isinstance(injected, list)
                else set()
            )
            variables["injected_memory_ids"] = sorted(existing_ids | returned_memory_ids)
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


def _memory_references(memories: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for memory in memories:
        memory_id = memory.get("id")
        if not isinstance(memory_id, str) or not memory_id:
            continue
        reference: dict[str, Any] = {
            "memory_id": memory_id,
            "rank": len(references) + 1,
        }
        for field in _REFERENCE_METADATA_FIELDS:
            if field in memory and memory[field] is not None:
                reference[field] = memory[field]
        references.append(reference)
    return references


def _valid_delivery_identity(delivery: Mapping[str, Any]) -> bool:
    request_id = delivery.get("recall_request_id")
    origin_turn_seq = delivery.get("origin_turn_seq")
    return (
        isinstance(request_id, str)
        and bool(request_id)
        and isinstance(origin_turn_seq, int)
        and not isinstance(origin_turn_seq, bool)
    )


def _delivery_memory_ids(delivery: Mapping[str, Any]) -> list[str]:
    references = delivery.get("references")
    if not isinstance(references, list):
        return []
    return [
        memory_id
        for reference in references
        if isinstance(reference, Mapping)
        and isinstance((memory_id := reference.get("memory_id")), str)
        and bool(memory_id)
    ]
