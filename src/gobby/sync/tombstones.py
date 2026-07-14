"""Shared tombstone helpers for JSONL synchronization."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.utils.datetime import datetime_to_iso, parse_stored_datetime

EntityType = Literal["task", "memory"]


def is_tombstone(record: dict[str, Any]) -> bool:
    """Return whether a JSONL record represents a deletion."""
    return record.get("_deleted") is True


def record_timestamp(record: dict[str, Any]) -> datetime | None:
    """Return the timestamp used for last-write-wins comparison."""
    raw = record.get("deleted_at") if is_tombstone(record) else record.get("updated_at")
    if raw is None:
        return None
    parsed = parse_stored_datetime(raw)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def newer_record(current: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Choose the newer live record or tombstone, preferring deletion on ties."""
    current_ts = record_timestamp(current)
    candidate_ts = record_timestamp(candidate)
    if candidate_ts is None:
        return current
    if current_ts is None or candidate_ts > current_ts:
        return candidate
    if candidate_ts == current_ts and is_tombstone(candidate):
        return candidate
    return current


def merge_jsonl_records(
    path: Path,
    records: list[dict[str, Any]],
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """Merge current records with an existing JSONL file by ID and timestamp."""
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        record_id = record["id"]
        current = merged.get(record_id)
        merged[record_id] = record if current is None else newer_record(current, record)
    if not path.exists():
        return list(merged.values())

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            candidate = json.loads(line)
            if not isinstance(candidate, dict) or not isinstance(candidate.get("id"), str):
                raise TypeError
            record_id = candidate["id"]
            current = merged.get(record_id)
            merged[record_id] = candidate if current is None else newer_record(current, candidate)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("Skipping malformed sync record at %s:%d", path, line_number)
    return list(merged.values())


def load_tombstones(
    db: HubDatabase,
    entity_type: EntityType,
    project_id: str | None,
    *,
    include_global: bool = False,
) -> list[dict[str, Any]]:
    """Load durable deletion markers as JSONL records."""
    query = "SELECT entity_id, project_id, deleted_at FROM sync_tombstones WHERE entity_type = %s"
    params: list[Any] = [entity_type]
    if project_id is not None:
        if include_global:
            query += " AND (project_id = %s OR project_id IS NULL)"
        else:
            query += " AND project_id = %s"
        params.append(project_id)

    records = []
    for row in db.fetchall(query, tuple(params)):
        deleted_at = parse_stored_datetime(row["deleted_at"])
        if deleted_at is None:
            continue
        timestamp = datetime_to_iso(deleted_at)
        records.append(
            {
                "id": str(row["entity_id"]),
                "project_id": str(row["project_id"]) if row["project_id"] else None,
                "_deleted": True,
                "deleted_at": timestamp,
                "updated_at": timestamp,
            }
        )
    return records


def apply_tombstone(
    conn: Transaction,
    entity_type: EntityType,
    entity_id: str,
    deleted_at: datetime,
) -> bool:
    """Delete a local row when the incoming tombstone wins LWW resolution."""
    if entity_type == "task":
        row = conn.execute("SELECT updated_at FROM tasks WHERE id = %s", (entity_id,)).fetchone()
    else:
        row = conn.execute("SELECT updated_at FROM memories WHERE id = %s", (entity_id,)).fetchone()
    if row is None:
        return False
    local_updated_at = parse_stored_datetime(row["updated_at"])
    if local_updated_at is not None:
        if local_updated_at.tzinfo is None:
            local_updated_at = local_updated_at.replace(tzinfo=UTC)
        if local_updated_at.astimezone(UTC) > deleted_at:
            return False

    if entity_type == "task":
        conn.execute(
            "DELETE FROM task_dependencies WHERE task_id = %s OR depends_on = %s",
            (entity_id, entity_id),
        )
        conn.execute(
            "UPDATE tasks SET parent_task_id = NULL WHERE parent_task_id = %s", (entity_id,)
        )
        conn.execute("DELETE FROM tasks WHERE id = %s", (entity_id,))
    else:
        conn.execute("DELETE FROM memories WHERE id = %s", (entity_id,))
    return True
