"""Durable delivery of self-directed memory recall references."""

from __future__ import annotations

import json
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
    """Persist and acknowledge ranked memory references for one session."""

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
        stored = self._variables.get_variables(session_id).get(
            MEMORY_RECALL_DELIVERIES_VARIABLE,
            [],
        )
        if not isinstance(stored, list):
            return []
        return [
            dict(delivery)
            for delivery in stored
            if isinstance(delivery, Mapping)
            and delivery.get("status") == _PENDING
            and _valid_delivery_identity(delivery)
            and _delivery_memory_ids(delivery)
        ]

    def acknowledge(
        self,
        session_id: str,
        deliveries: Sequence[Mapping[str, Any]],
    ) -> None:
        """Atomically complete unchanged deliveries and track their memory IDs."""
        expected = {
            delivery["origin_turn_seq"]: delivery["recall_request_id"]
            for delivery in deliveries
            if _valid_delivery_identity(delivery)
        }
        if not expected:
            return

        now = datetime.now(UTC).isoformat()
        with self._database.transaction_immediate(
            SessionVariableMutation(session_id=session_id)
        ) as conn:
            row = conn.execute(
                "SELECT variables FROM session_variables WHERE session_id = %s",
                (session_id,),
            ).fetchone()
            if not row:
                return
            variables = _decode_variables_payload(row["variables"])
            stored = variables.get(MEMORY_RECALL_DELIVERIES_VARIABLE, [])
            if not isinstance(stored, list):
                return

            acknowledged_ids: set[str] = set()
            updated: list[Any] = []
            for delivery in stored:
                if not isinstance(delivery, Mapping):
                    updated.append(delivery)
                    continue
                origin_turn_seq = delivery.get("origin_turn_seq")
                request_id = delivery.get("recall_request_id")
                if expected.get(origin_turn_seq) != request_id:
                    updated.append(dict(delivery))
                    continue
                completed = dict(delivery)
                completed["status"] = _COMPLETE
                updated.append(completed)
                acknowledged_ids.update(_delivery_memory_ids(delivery))

            if not acknowledged_ids:
                return
            variables[MEMORY_RECALL_DELIVERIES_VARIABLE] = updated
            injected = variables.get("injected_memory_ids", [])
            existing_ids = (
                {value for value in injected if isinstance(value, str) and value}
                if isinstance(injected, list)
                else set()
            )
            variables["injected_memory_ids"] = sorted(existing_ids | acknowledged_ids)
            conn.execute(
                "UPDATE session_variables SET variables = %s, updated_at = %s "
                "WHERE session_id = %s",
                (_encode_variables_payload(variables), now, session_id),
            )


def render_memory_recall_deliveries(deliveries: Sequence[Mapping[str, Any]]) -> str:
    """Render exact retrieval instructions for pending recall references."""
    blocks: list[str] = []
    for delivery in deliveries:
        if not _valid_delivery_identity(delivery):
            continue
        references = delivery.get("references")
        if not isinstance(references, list):
            continue
        lines = [
            "[Pending memory recall references]",
            (
                f"recall_request_id={delivery['recall_request_id']} "
                f"origin_turn_seq={delivery['origin_turn_seq']} "
                f"project_id={delivery.get('project_id')}"
            ),
            (
                "Retrieve each selected memory in order through "
                "`gobby-memory.get_memory(memory_id=...)`:"
            ),
        ]
        for reference in references:
            if not isinstance(reference, Mapping):
                continue
            memory_id = reference.get("memory_id")
            if not isinstance(memory_id, str) or not memory_id:
                continue
            rank = reference.get("rank")
            metadata = [f"rank={rank}"]
            metadata.extend(
                f"{field}={_format_metadata_value(reference[field])}"
                for field in _REFERENCE_METADATA_FIELDS
                if field in reference
            )
            call = f"gobby-memory.get_memory(memory_id={json.dumps(memory_id)})"
            lines.append(f"{rank}. `{call}` ({', '.join(metadata)})")
        if len(lines) > 3:
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


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


def _format_metadata_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), sort_keys=True)
