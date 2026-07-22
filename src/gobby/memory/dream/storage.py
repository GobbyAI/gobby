"""Persistent run and snapshot storage for memory dream."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import older_than_now_expr
from gobby.utils.datetime import to_json_safe, utc_now

PLATFORM_TRUTH_SCOPE = "__gobby_platform__"

# Columns added by migration 289 (dream soft-delete). Snapshots taken before
# 289 lack them, so restore_memory_row defaults them to NULL instead of failing.
_DREAM_SOFT_DELETE_COLUMNS = ("deleted_at", "dream_action", "last_dreamed_at")
_MEMORY_COLUMNS = (
    "id",
    "project_id",
    "is_global",
    "memory_type",
    "content",
    "source_type",
    "source_session_id",
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
RESTORE_MEMORY_SQL = f"""
INSERT INTO memories ({_MEMORY_COLUMN_LIST})
VALUES ({_MEMORY_PLACEHOLDERS})
ON CONFLICT (id) DO UPDATE SET {_RESTORE_MEMORY_ASSIGNMENTS}
"""
_RUN_JSON_COLUMNS = frozenset({"options", "plan", "summary"})
_RUN_UPDATE_SET_CLAUSES = {
    "project_id": "project_id = %s",
    "status": "status = %s",
    "dry_run": "dry_run = %s",
    "options": "options = %s",
    "plan": "plan = %s",
    "summary": "summary = %s",
    "error": "error = %s",
    "started_at": "started_at = %s",
    "completed_at": "completed_at = %s",
    "reverted_at": "reverted_at = %s",
    "created_at": "created_at = %s",
    "updated_at": "updated_at = %s",
}

# Error recorded on runs reconciled to 'interrupted' after a daemon restart.
INTERRUPTED_RESTART_ERROR = "Interrupted: daemon restarted while the dream run was in progress"
# Error recorded when an in-flight run is cancelled (shutdown/timeout) before completing.
INTERRUPTED_CANCELLED_ERROR = "Interrupted: dream run cancelled before completion"


@dataclass(frozen=True, slots=True)
class DreamApplyResult:
    """Rows captured by one committed fenced dream action."""

    snapshot_id: int
    before: dict[str, Any]
    after: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DreamRevertResult:
    """Outcome of one snapshot's conflict-aware primary restore."""

    status: Literal["restored", "conflict", "missing"]
    memory_id: str
    row: dict[str, Any] | None = None


class MemoryDreamStore:
    """Store memory dream runs and exact mutation snapshots."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def ensure_schema(self) -> None:
        """Create dream tables for upgraded daemons that have not migrated yet."""
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_dream_runs (
                id UUID PRIMARY KEY,
                project_id UUID REFERENCES projects(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'started'
                    CONSTRAINT memory_dream_runs_status_check
                    CHECK (
                        status IN (
                            'started', 'running', 'completed', 'failed', 'reverted',
                            'revert_failed', 'interrupted'
                        )
                    ),
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
                run_id UUID NOT NULL REFERENCES memory_dream_runs(id)
                    ON DELETE CASCADE DEFERRABLE INITIALLY IMMEDIATE,
                memory_id UUID NOT NULL,
                action TEXT NOT NULL
                    CONSTRAINT memory_dream_snapshots_action_check
                    CHECK (
                        action IN (
                            'keep', 'delete', 'refresh', 'merge', 'supersede', 'review',
                            'promote'
                        )
                    ),
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
            ON memory_dream_snapshots(run_id)
            """
        )
        self.db.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                      FROM pg_class cls
                      JOIN pg_attribute attr
                        ON attr.attrelid = cls.oid
                       AND attr.attname = 'status'
                      JOIN pg_attrdef def
                        ON def.adrelid = attr.attrelid
                       AND def.adnum = attr.attnum
                     WHERE cls.oid = 'memory_dream_runs'::regclass
                       AND pg_get_expr(def.adbin, def.adrelid) = '''started''::text'
                ) THEN
                    ALTER TABLE memory_dream_runs ALTER COLUMN status SET DEFAULT 'started';
                END IF;
            END $$;
            """
        )
        self.db.execute(
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                      FROM pg_constraint
                     WHERE conname = 'memory_dream_snapshots_action_check'
                       AND conrelid = 'memory_dream_snapshots'::regclass
                ) THEN
                    ALTER TABLE memory_dream_snapshots
                        ADD CONSTRAINT memory_dream_snapshots_action_check
                        CHECK (
                            action IN (
                                'keep', 'delete', 'refresh', 'merge', 'supersede', 'review',
                                'promote'
                            )
                        );
                ELSIF EXISTS (
                    SELECT 1
                      FROM pg_constraint
                     WHERE conname = 'memory_dream_snapshots_action_check'
                       AND conrelid = 'memory_dream_snapshots'::regclass
                       AND pg_get_constraintdef(oid) NOT LIKE '%promote%'
                ) THEN
                    ALTER TABLE memory_dream_snapshots
                        DROP CONSTRAINT memory_dream_snapshots_action_check;
                    ALTER TABLE memory_dream_snapshots
                        ADD CONSTRAINT memory_dream_snapshots_action_check
                        CHECK (
                            action IN (
                                'keep', 'delete', 'refresh', 'merge', 'supersede', 'review',
                                'promote'
                            )
                        );
                END IF;
            END $$;
            """
        )
        self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_dream_truth_state (
                project_id TEXT PRIMARY KEY,
                digest_hash TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
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
        unknown_fields = sorted(set(fields) - set(_RUN_UPDATE_SET_CLAUSES))
        if unknown_fields:
            raise ValueError(
                "Unsupported memory_dream_runs update field(s): " + ", ".join(unknown_fields)
            )
        fields["updated_at"] = _now()
        encoded = {
            key: _json(value) if key in _RUN_JSON_COLUMNS else value
            for key, value in fields.items()
        }
        # Column assignments are selected from _RUN_UPDATE_SET_CLAUSES above;
        # values remain parameterized with psycopg placeholders.
        set_clause = ", ".join(_RUN_UPDATE_SET_CLAUSES[key] for key in encoded)
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

    def get_truth_digest_hash(self, project_id: str) -> str | None:
        """Return the last-seen codewiki truth-digest hash for a project."""
        row = self.db.fetchone(
            "SELECT digest_hash FROM memory_dream_truth_state WHERE project_id = %s",
            (project_id,),
        )
        return str(row["digest_hash"]) if row is not None else None

    def get_platform_truth_digest_hash(self) -> str | None:
        """Return the last-seen platform truth digest hash."""
        return self.get_truth_digest_hash(PLATFORM_TRUTH_SCOPE)

    def set_truth_digest_hash(self, project_id: str, digest_hash: str) -> None:
        """Record the current codewiki truth-digest hash for a project."""
        self.db.execute(
            """
            INSERT INTO memory_dream_truth_state (project_id, digest_hash, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (project_id) DO UPDATE
                SET digest_hash = EXCLUDED.digest_hash,
                    updated_at = EXCLUDED.updated_at
            """,
            (project_id, digest_hash),
        )

    def set_platform_truth_digest_hash(self, digest_hash: str) -> None:
        """Record the current platform truth digest hash."""
        self.set_truth_digest_hash(PLATFORM_TRUTH_SCOPE, digest_hash)

    def mark_interrupted_runs(self, *, error: str = INTERRUPTED_RESTART_ERROR) -> list[str]:
        """Reconcile runs orphaned in a non-terminal state to 'interrupted'.

        A dream run executes as an in-process asyncio background task with no
        external liveness handle, so a daemon restart cancels it without
        persisting a terminal status, leaving the row at 'running'/'started'.
        The caller must invoke this once during synchronous startup, before the
        HTTP server accepts requests or any new run is scheduled, so every
        non-terminal row is necessarily orphaned and no live run is clobbered.

        Returns the reconciled run IDs.
        """
        rows = self.db.fetchall(
            "SELECT id FROM memory_dream_runs WHERE status IN ('started', 'running')"
        )
        run_ids = [str(row["id"]) for row in rows]
        completed_at = _now()
        for run_id in run_ids:
            self.update_run(
                run_id,
                status="interrupted",
                completed_at=completed_at,
                error=error,
            )
        return run_ids

    def get_memory_row(self, memory_id: str) -> dict[str, Any] | None:
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

    @staticmethod
    def _crossrefs_with_connection(conn: Any, memory_id: str) -> list[dict[str, Any]]:
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

    @staticmethod
    def _insert_snapshot_with_connection(
        conn: Any,
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

    @staticmethod
    def _complete_snapshot_with_connection(
        conn: Any,
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

    def apply_candidate_action(
        self,
        *,
        run_id: str,
        memory_id: str,
        action: Literal["keep", "review", "delete", "refresh", "promote"],
        selected_due_version: int,
        selected_updated_at: datetime,
        selected_project_id: str,
        selected_is_global: bool,
        stamp: str,
        content: str | None = None,
        tags: list[str] | None = None,
        on_committed: Callable[[], None] | None = None,
    ) -> DreamApplyResult | None:
        """Apply one dream action behind the complete selected-row fence."""
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM memories
                 WHERE id = %s
                   AND dream_due_version = %s
                   AND updated_at = %s
                   AND project_id = %s
                   AND is_global = %s
                   AND deleted_at IS NULL
                 FOR UPDATE
                """,
                (
                    memory_id,
                    selected_due_version,
                    selected_updated_at,
                    selected_project_id,
                    selected_is_global,
                ),
            ).fetchone()
            if row is None:
                return None

            before = dict(row)
            before["tags"] = _decode(before.get("tags")) or []
            before["_crossrefs"] = self._crossrefs_with_connection(conn, memory_id)
            snapshot_id = self._insert_snapshot_with_connection(
                conn,
                run_id=run_id,
                memory_id=memory_id,
                action=action,
                before_data=before,
            )

            if action == "keep":
                conn.execute(
                    "UPDATE memories SET last_dreamed_at = %s WHERE id = %s",
                    (stamp, memory_id),
                )
            elif action in {"review", "delete"}:
                conn.execute(
                    """
                    UPDATE memories
                       SET last_dreamed_at = %s, deleted_at = %s, dream_action = %s
                     WHERE id = %s
                    """,
                    (stamp, stamp, action, memory_id),
                )
            elif action == "refresh":
                normalized_content = (content or "").strip()
                if not normalized_content:
                    raise ValueError("Memory content cannot be empty")
                duplicate = conn.execute(
                    """
                    SELECT id FROM memories
                     WHERE content = %s
                       AND project_id = %s
                       AND is_global = %s
                       AND id != %s
                     LIMIT 1
                    """,
                    (normalized_content, selected_project_id, selected_is_global, memory_id),
                ).fetchone()
                if duplicate is not None:
                    raise ValueError("Memory content already exists in this project/global scope")
                if tags is None:
                    conn.execute(
                        """
                        UPDATE memories
                           SET content = %s, updated_at = %s, last_dreamed_at = %s,
                               vector_needs_reindex = TRUE
                         WHERE id = %s
                        """,
                        (normalized_content, utc_now(), stamp, memory_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE memories
                           SET content = %s, tags = %s, updated_at = %s,
                               last_dreamed_at = %s, vector_needs_reindex = TRUE
                         WHERE id = %s
                        """,
                        (normalized_content, _json(tags), utc_now(), stamp, memory_id),
                    )
            else:
                conn.execute(
                    """
                    UPDATE memories
                       SET is_global = TRUE, vector_needs_reindex = TRUE,
                           last_dreamed_at = %s
                     WHERE id = %s
                    """,
                    (stamp, memory_id),
                )

            after_row = conn.execute(
                "SELECT * FROM memories WHERE id = %s", (memory_id,)
            ).fetchone()
            if after_row is None:
                raise RuntimeError(f"Memory {memory_id} vanished during dream apply")
            after = dict(after_row)
            after["tags"] = _decode(after.get("tags")) or []
            after["_crossrefs"] = self._crossrefs_with_connection(conn, memory_id)
            self._complete_snapshot_with_connection(
                conn,
                snapshot_id,
                after_data=after,
            )

        if on_committed is not None:
            on_committed()
        return DreamApplyResult(snapshot_id=snapshot_id, before=before, after=after)

    @staticmethod
    def _same_snapshot_value(current: Any, captured: Any) -> bool:
        return json.dumps(to_json_safe(current), default=str, sort_keys=True) == json.dumps(
            to_json_safe(captured),
            default=str,
            sort_keys=True,
        )

    @staticmethod
    def _restore_crossrefs_with_connection(
        conn: Any,
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

    def revert_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        on_committed: Callable[[], None] | None = None,
    ) -> DreamRevertResult:
        """Restore one snapshot only if its action-owned after-state still owns the row."""
        memory_id = str(snapshot["memory_id"])
        action = str(snapshot["action"])
        before = snapshot.get("before_data")
        after = snapshot.get("after_data")
        if not isinstance(before, dict) or not isinstance(after, dict):
            raise ValueError(f"Snapshot {snapshot.get('id')} lacks restorable row data")

        owned_columns: tuple[str, ...]
        if action == "refresh":
            owned_columns = ("content", "tags")
        elif action == "promote":
            owned_columns = ("is_global",)
        elif action in {"review", "delete"}:
            owned_columns = ("deleted_at", "dream_action")
        else:
            owned_columns = ()

        with self.db.transaction() as conn:
            current_row = conn.execute(
                "SELECT * FROM memories WHERE id = %s FOR UPDATE",
                (memory_id,),
            ).fetchone()
            if current_row is None:
                return DreamRevertResult(status="missing", memory_id=memory_id)
            current = dict(current_row)
            current["tags"] = _decode(current.get("tags")) or []
            fence_columns = tuple(
                dict.fromkeys(("deleted_at", "project_id", "is_global", *owned_columns))
            )
            if any(
                not self._same_snapshot_value(current.get(column), after.get(column))
                for column in fence_columns
            ):
                return DreamRevertResult(status="conflict", memory_id=memory_id)

            assignments: list[str] = []
            params: list[Any] = []
            for column in owned_columns:
                assignments.append(f"{column} = %s")
                value = before.get(column)
                params.append(_json(value) if column == "tags" else value)
            assignments.extend(
                [
                    "updated_at = %s",
                    "last_dreamed_at = NULL",
                    "dream_due_version = dream_due_version + 1",
                    "vector_needs_reindex = TRUE",
                ]
            )
            params.extend((utc_now(), memory_id))
            conn.execute(
                f"UPDATE memories SET {', '.join(assignments)} WHERE id = %s",  # nosec B608
                tuple(params),
            )
            self._restore_crossrefs_with_connection(
                conn,
                memory_id,
                list(before.get("_crossrefs") or []),
            )
            restored_row = conn.execute(
                "SELECT * FROM memories WHERE id = %s",
                (memory_id,),
            ).fetchone()
            if restored_row is None:
                raise RuntimeError(f"Memory {memory_id} vanished during dream revert")
            restored = dict(restored_row)
            restored["tags"] = _decode(restored.get("tags")) or []

        if on_committed is not None:
            on_committed()
        return DreamRevertResult(status="restored", memory_id=memory_id, row=restored)

    def _get_crossref_rows(self, memory_id: str) -> list[dict[str, Any]]:
        rows = self.db.fetchall(
            """
            SELECT source_id, target_id, similarity, created_at
              FROM memory_crossrefs
             WHERE source_id = %s OR target_id = %s
            """,
            (memory_id, memory_id),
        )
        return [dict(row) for row in rows]

    def restore_crossrefs(self, memory_rows: list[dict[str, Any]]) -> None:
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

    def transfer_crossrefs(self, duplicate_id: str, keeper_id: str) -> int:
        """Copy a duplicate's crossrefs to the keeper before cascade deletion."""
        transferred = 0
        for crossref in self._get_crossref_rows(duplicate_id):
            source_id = (
                keeper_id if crossref["source_id"] == duplicate_id else crossref["source_id"]
            )
            target_id = (
                keeper_id if crossref["target_id"] == duplicate_id else crossref["target_id"]
            )
            if source_id == target_id:
                continue
            self.db.execute(
                """
                INSERT INTO memory_crossrefs (source_id, target_id, similarity, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(source_id, target_id) DO UPDATE SET
                    similarity = GREATEST(memory_crossrefs.similarity, excluded.similarity)
                """,
                (source_id, target_id, crossref["similarity"], crossref["created_at"]),
            )
            transferred += 1
        return transferred

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
        if row is None:
            raise RuntimeError("memory_dream_snapshots insert did not return an id")
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
        missing = [
            column
            for column in _MEMORY_COLUMNS
            if column not in data and column not in _DREAM_SOFT_DELETE_COLUMNS
        ]
        if missing:
            raise ValueError(
                f"Cannot restore memory row with missing columns: {', '.join(missing)}"
            )
        # data.get() defaults the dream soft-delete columns to NULL on pre-289 snapshots.
        values = {column: data.get(column) for column in _MEMORY_COLUMNS}
        values["tags"] = _json(values.get("tags") or [])
        self.db.execute(
            RESTORE_MEMORY_SQL,
            tuple(values[column] for column in _MEMORY_COLUMNS),
        )

    def delete_memory_row(self, memory_id: str) -> None:
        self.db.execute("DELETE FROM memories WHERE id = %s", (memory_id,))

    def prune_runs(self, older_than_days: int) -> int:
        """Delete dream runs older than the retention window.

        Snapshots are reclaimed automatically: ``memory_dream_snapshots.run_id``
        carries an ``ON DELETE CASCADE`` foreign key, so removing aged runs drops
        their snapshot rows in the same statement. Returns the run count removed.
        """
        if older_than_days <= 0:
            raise ValueError("older_than_days must be positive")
        cutoff = older_than_now_expr(self.db, "created_at", "%s", "day")
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"DELETE FROM memory_dream_runs WHERE {cutoff} RETURNING id",  # nosec B608
                (older_than_days,),
            ).fetchall()
        return len(rows)


def _json(value: Any) -> str | None:
    if value is None:
        return None
    # Snapshot payloads carry raw psycopg rows whose TIMESTAMPTZ columns are
    # datetime objects; convert them to ISO strings before dumping.
    return json.dumps(to_json_safe(value))


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
