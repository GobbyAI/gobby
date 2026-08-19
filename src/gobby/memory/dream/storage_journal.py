"""Snapshot journal and memory restoration storage."""

from __future__ import annotations

import json
from typing import Any, Protocol

from gobby.storage.embedding_generation_state import EmbeddingGenerationState
from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.utils.json_helpers import json_dumps

_DREAM_SOFT_DELETE_COLUMNS = ("deleted_at", "dream_action", "last_dreamed_at")
_OPTIONAL_MEMORY_COLUMNS = (
    *_DREAM_SOFT_DELETE_COLUMNS,
    "rationale",
    "source_task_id",
    "created_by_agent",
)
_MEMORY_COLUMNS = (
    "id",
    "project_id",
    "is_global",
    "memory_type",
    "content",
    "source_type",
    "source_session_id",
    "rationale",
    "source_task_id",
    "created_by_agent",
    "access_count",
    "last_accessed_at",
    "tags",
    "graph_processed",
    "created_at",
    "updated_at",
    *_DREAM_SOFT_DELETE_COLUMNS,
)
_MEMORY_COLUMN_LIST = ", ".join(_MEMORY_COLUMNS)
_MEMORY_PLACEHOLDERS = ", ".join(["%s"] * len(_MEMORY_COLUMNS))
_RESTORE_MEMORY_ASSIGNMENTS = ", ".join(
    f"{column} = EXCLUDED.{column}" for column in _MEMORY_COLUMNS[1:]
)
_RESTORE_MEMORY_SQL = f"""
INSERT INTO memories ({_MEMORY_COLUMN_LIST})
VALUES ({_MEMORY_PLACEHOLDERS})
ON CONFLICT (id) DO UPDATE SET {_RESTORE_MEMORY_ASSIGNMENTS}
"""


class _DreamJournalHost(Protocol):
    db: HubDatabase

    def _get_crossref_rows(self, memory_id: str) -> list[dict[str, Any]]: ...


def capture_crossrefs(
    conn: Transaction,
    memory_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT source_id, target_id, similarity, created_at
          FROM memory_crossrefs
         WHERE source_id = %s OR target_id = %s
         ORDER BY source_id, target_id
        """,
        (memory_id, memory_id),
    ).fetchall()
    return [dict(row) for row in rows]


def insert_snapshot(
    conn: Transaction,
    *,
    run_id: str,
    memory_id: str,
    action: str,
    before_data: dict[str, Any] | None,
) -> int:
    row = conn.execute(
        """
        INSERT INTO memory_dream_snapshots (
            run_id, memory_id, action, before_data, applied
        )
        VALUES (%s, %s, %s, %s, FALSE)
        RETURNING id
        """,
        (run_id, memory_id, action, _json(before_data)),
    ).fetchone()
    if row is None:
        raise RuntimeError("memory_dream_snapshots insert did not return an id")
    return int(row["id"])


def complete_snapshot(
    conn: Transaction,
    snapshot_id: int,
    *,
    after_data: dict[str, Any] | None,
) -> None:
    conn.execute(
        """
        UPDATE memory_dream_snapshots
           SET after_data = %s, applied = TRUE
         WHERE id = %s
        """,
        (_json(after_data), snapshot_id),
    )


def restore_crossrefs(
    conn: Transaction,
    memory_id: str,
    crossrefs: list[dict[str, Any]],
) -> None:
    conn.execute(
        "DELETE FROM memory_crossrefs WHERE source_id = %s OR target_id = %s",
        (memory_id, memory_id),
    )
    for crossref in crossrefs:
        conn.execute(
            """
            INSERT INTO memory_crossrefs (source_id, target_id, similarity, created_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (source_id, target_id) DO UPDATE
            SET similarity = EXCLUDED.similarity,
                created_at = EXCLUDED.created_at
            """,
            (
                crossref["source_id"],
                crossref["target_id"],
                crossref["similarity"],
                crossref["created_at"],
            ),
        )


class _DreamJournalMixin:
    def get_memory_row(
        self: _DreamJournalHost,
        memory_id: str,
    ) -> dict[str, Any] | None:
        row = self.db.fetchone(
            "SELECT * FROM memories WHERE id = %s",
            (memory_id,),
        )
        if row is None:
            return None
        data = dict(row)
        data["tags"] = _decode(data.get("tags")) or []
        data["_crossrefs"] = self._get_crossref_rows(memory_id)
        return data

    def _get_crossref_rows(
        self: _DreamJournalHost,
        memory_id: str,
    ) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT source_id, target_id, similarity, created_at
              FROM memory_crossrefs
             WHERE source_id = %s OR target_id = %s
            """,
            (memory_id, memory_id),
        )
        return [dict(row) for row in rows]

    def restore_crossrefs(
        self: _DreamJournalHost,
        memory_rows: list[dict[str, Any]],
    ) -> None:
        """Restore the exact crossref set captured for the supplied memory rows."""
        memory_ids = {str(row["id"]) for row in memory_rows}
        desired: dict[tuple[str, str], dict[str, Any]] = {}
        for row in memory_rows:
            for crossref in row.get("_crossrefs", []):
                key = (str(crossref["source_id"]), str(crossref["target_id"]))
                current = desired.get(key)
                if current is None or float(crossref["similarity"]) > float(current["similarity"]):
                    desired[key] = crossref

        for memory_id in memory_ids:
            self.db.execute(
                "DELETE FROM memory_crossrefs WHERE source_id = %s OR target_id = %s",
                (memory_id, memory_id),
            )
        for crossref in desired.values():
            self.db.execute(
                """
                INSERT INTO memory_crossrefs (source_id, target_id, similarity, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(source_id, target_id) DO UPDATE SET
                    similarity = excluded.similarity,
                    created_at = excluded.created_at
                """,
                (
                    crossref["source_id"],
                    crossref["target_id"],
                    crossref["similarity"],
                    crossref["created_at"],
                ),
            )

    def insert_snapshot(
        self: _DreamJournalHost,
        *,
        run_id: str,
        memory_id: str,
        action: str,
        before_data: dict[str, Any] | None,
    ) -> int:
        row = self.db.fetchone(
            """
            INSERT INTO memory_dream_snapshots (
                run_id, memory_id, action, before_data, applied
            )
            VALUES (%s, %s, %s, %s, FALSE)
            RETURNING id
            """,
            (run_id, memory_id, action, _json(before_data)),
        )
        if row is None:
            raise RuntimeError("memory_dream_snapshots insert did not return an id")
        return int(row["id"])

    def complete_snapshot(
        self: _DreamJournalHost,
        snapshot_id: int,
        *,
        after_data: dict[str, Any] | None,
    ) -> None:
        self.db.execute(
            """
            UPDATE memory_dream_snapshots
               SET after_data = %s, applied = TRUE
             WHERE id = %s
            """,
            (_json(after_data), snapshot_id),
        )

    def count_snapshots(self: _DreamJournalHost, run_id: str) -> int:
        row = self.db.fetchone(
            """
            SELECT COUNT(*) AS total
              FROM memory_dream_snapshots
             WHERE run_id = %s AND applied = TRUE
            """,
            (run_id,),
        )
        return 0 if row is None else int(row["total"])

    def list_snapshots(
        self: _DreamJournalHost,
        run_id: str,
    ) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT * FROM memory_dream_snapshots
             WHERE run_id = %s AND applied = TRUE
             ORDER BY id DESC
            """,
            (run_id,),
        )
        snapshots: list[dict[str, Any]] = []
        for row in rows:
            snapshot = dict(row)
            snapshot["before_data"] = _decode(snapshot.get("before_data"))
            snapshot["after_data"] = _decode(snapshot.get("after_data"))
            snapshots.append(snapshot)
        return snapshots

    def restore_memory_row(
        self: _DreamJournalHost,
        data: dict[str, Any],
    ) -> None:
        missing = [
            column
            for column in _MEMORY_COLUMNS
            if column not in data and column not in _OPTIONAL_MEMORY_COLUMNS
        ]
        if missing:
            raise ValueError(
                f"Cannot restore memory row with missing columns: {', '.join(missing)}"
            )
        # data.get() defaults the dream soft-delete columns to NULL on pre-289 snapshots.
        values = {column: data.get(column) for column in _MEMORY_COLUMNS}
        values["tags"] = _json(values.get("tags") or [])
        with self.db.transaction() as conn:
            conn.execute(
                _RESTORE_MEMORY_SQL,
                tuple(values[column] for column in _MEMORY_COLUMNS),
            )
            EmbeddingGenerationState(self.db).append_change(
                "memory",
                str(values["id"]),
                is_tombstone=values.get("deleted_at") is not None,
                transaction=conn,
            )

    def delete_memory_row(
        self: _DreamJournalHost,
        memory_id: str,
    ) -> None:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM memories WHERE id = %s", (memory_id,))
            if cursor.rowcount:
                EmbeddingGenerationState(self.db).append_change(
                    "memory", memory_id, is_tombstone=True, transaction=conn
                )


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json_dumps(value)


def _decode(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value
