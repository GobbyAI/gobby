"""Persistent run and snapshot storage for memory dream."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from gobby.storage.hub.protocol import HubDatabase

_MEMORY_COLUMNS = (
    "id",
    "project_id",
    "memory_type",
    "content",
    "source_type",
    "source_session_id",
    "access_count",
    "last_accessed_at",
    "tags",
    "media",
    "graph_processed",
    "created_at",
    "updated_at",
)


class MemoryDreamStore:
    """Store memory dream runs and exact mutation snapshots."""

    def __init__(self, db: HubDatabase):
        self.db = db
        self.ensure_schema()

    def ensure_schema(self) -> None:
        """Create dream tables for upgraded daemons that have not migrated yet."""
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_dream_runs (
                id TEXT PRIMARY KEY,
                project_id TEXT,
                status TEXT NOT NULL,
                dry_run BOOLEAN NOT NULL DEFAULT FALSE,
                options JSONB NOT NULL DEFAULT '{}'::jsonb,
                plan JSONB,
                summary JSONB,
                error TEXT,
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                reverted_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_dream_snapshots (
                id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES memory_dream_runs(id)
                    ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
                memory_id TEXT NOT NULL,
                action TEXT NOT NULL,
                before_data JSONB,
                after_data JSONB,
                applied BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        self.db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_memory_dream_snapshots_run
            ON memory_dream_snapshots(run_id, id)
            """
        )

    def create_run(
        self,
        *,
        project_id: str | None,
        dry_run: bool,
        options: dict[str, Any],
    ) -> str:
        run_id = str(uuid4())
        now = _now()
        self.db.execute(
            """
            INSERT INTO memory_dream_runs (
                id, project_id, status, dry_run, options, started_at, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (run_id, project_id, "running", dry_run, _json(options), now, now, now),
        )
        return run_id

    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get_run(run_id)
        fields["updated_at"] = _now()
        encoded = {
            key: _json(value) if key in {"options", "plan", "summary"} else value
            for key, value in fields.items()
        }
        set_clause = ", ".join(f"{key} = %s" for key in encoded)
        self.db.execute(
            f"UPDATE memory_dream_runs SET {set_clause} WHERE id = %s",  # nosec B608
            tuple(encoded.values()) + (run_id,),
        )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.db.fetchone("SELECT * FROM memory_dream_runs WHERE id = %s", (run_id,))
        if row is None:
            return None
        result = dict(row)
        for key in ("options", "plan", "summary"):
            result[key] = _decode(result.get(key))
        return result

    def get_memory_row(self, memory_id: str) -> dict[str, Any] | None:
        row = self.db.fetchone(
            f"SELECT {', '.join(_MEMORY_COLUMNS)} FROM memories WHERE id = %s",  # nosec B608
            (memory_id,),
        )
        if row is None:
            return None
        data = dict(row)
        data["tags"] = _decode(data.get("tags")) or []
        data["media"] = _decode(data.get("media"))
        return data

    def insert_snapshot(
        self,
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
        return int(row["id"])

    def complete_snapshot(
        self,
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

    def record_applied_snapshot(
        self,
        *,
        run_id: str,
        memory_id: str,
        action: str,
        before_data: dict[str, Any] | None,
        after_data: dict[str, Any] | None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO memory_dream_snapshots (
                run_id, memory_id, action, before_data, after_data, applied
            )
            VALUES (%s, %s, %s, %s, %s, TRUE)
            """,
            (run_id, memory_id, action, _json(before_data), _json(after_data)),
        )

    def list_snapshots(self, run_id: str) -> list[dict[str, Any]]:
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

    def restore_memory_row(self, data: dict[str, Any]) -> None:
        values = {column: data.get(column) for column in _MEMORY_COLUMNS}
        values["tags"] = _json(values.get("tags") or [])
        values["media"] = _json(values.get("media"))
        assignments = ", ".join(f"{column} = EXCLUDED.{column}" for column in _MEMORY_COLUMNS[1:])
        self.db.execute(
            f"""
            INSERT INTO memories ({", ".join(_MEMORY_COLUMNS)})
            VALUES ({", ".join(["%s"] * len(_MEMORY_COLUMNS))})
            ON CONFLICT (id) DO UPDATE SET {assignments}
            """,  # nosec B608
            tuple(values[column] for column in _MEMORY_COLUMNS),
        )

    def delete_memory_row(self, memory_id: str) -> None:
        self.db.execute("DELETE FROM memories WHERE id = %s", (memory_id,))


def _json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value)


def _decode(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat()
