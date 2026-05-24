"""Storage manager for task expansion runs."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)

ExpansionRunStatus = Literal[
    "pending",
    "running",
    "compiled",
    "applying",
    "completed",
    "failed",
    "cancelled",
]
ExpansionInputSource = Literal["task", "plan"]


@dataclass
class ExpansionRun:
    """Expansion run data model."""

    id: str
    parent_task_id: str
    project_id: str
    triggering_session_id: str | None
    status: ExpansionRunStatus
    input_source: ExpansionInputSource
    created_at: str
    updated_at: str
    plan_file: str | None = None
    provider: str | None = None
    model: str | None = None
    options: dict[str, Any] | None = None
    compiled_spec: dict[str, Any] | None = None
    qa_result: dict[str, Any] | None = None
    task_id_map: dict[str, str] | None = None
    created_task_ids: list[str] | None = None
    error: str | None = None
    logs: list[dict[str, Any]] | None = None
    checkpoints: dict[str, Any] | None = None
    started_at: str | None = None
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ExpansionRun:
        """Create an ExpansionRun from a database row."""
        run_id = row["id"]

        def _decode(field: str) -> Any:
            raw = row[field]
            if not raw:
                return None
            if isinstance(raw, dict | list):
                return raw
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"ExpansionRun.from_row: failed to decode {field} for run {run_id}: {exc}"
                ) from exc

        return cls(
            id=run_id,
            parent_task_id=row["parent_task_id"],
            project_id=row["project_id"],
            triggering_session_id=row["triggering_session_id"],
            status=row["status"],
            input_source=row["input_source"],
            plan_file=row["plan_file"],
            provider=row["provider"],
            model=row["model"],
            options=_decode("options_json"),
            compiled_spec=_decode("compiled_spec_json"),
            qa_result=_decode("qa_result_json"),
            task_id_map=_decode("task_id_map_json"),
            created_task_ids=_decode("created_task_ids_json"),
            error=row["error"],
            logs=_decode("logs_json"),
            checkpoints=_decode("checkpoints_json"),
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "run_id": self.id,
            "id": self.id,
            "parent_task_id": self.parent_task_id,
            "project_id": self.project_id,
            "triggering_session_id": self.triggering_session_id,
            "status": self.status,
            "input_source": self.input_source,
            "plan_file": self.plan_file,
            "provider": self.provider,
            "model": self.model,
            "options": self.options,
            "compiled_spec": self.compiled_spec,
            "qa_result": self.qa_result,
            "task_id_map": self.task_id_map,
            "created_task_ids": self.created_task_ids,
            "error": self.error,
            "logs": self.logs,
            "checkpoints": self.checkpoints,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class LocalExpansionRunManager:
    """Manager for expansion run storage operations."""

    _ACTIVE_STATUSES: tuple[ExpansionRunStatus, ...] = (
        "pending",
        "running",
        "compiled",
        "applying",
    )

    def __init__(self, db: HubDatabase):
        self.db = db

    def create(
        self,
        *,
        parent_task_id: str,
        project_id: str,
        triggering_session_id: str | None,
        input_source: ExpansionInputSource,
        plan_file: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        options: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> ExpansionRun:
        """Create a new expansion run."""
        if run_id is None:
            run_id = f"expand-{uuid.uuid4().hex[:12]}"
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            INSERT INTO expansion_runs (
                id, parent_task_id, project_id, triggering_session_id, status,
                input_source, plan_file, provider, model, options_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                parent_task_id,
                project_id,
                triggering_session_id,
                input_source,
                plan_file,
                provider,
                model,
                json.dumps(options) if options is not None else None,
                now,
                now,
            ),
        )
        run = self.get(run_id)
        if run is None:
            raise RuntimeError(f"Failed to retrieve expansion run {run_id}")
        return run

    def get(self, run_id: str) -> ExpansionRun | None:
        """Get an expansion run by ID."""
        row = self.db.fetchone("SELECT * FROM expansion_runs WHERE id = ?", (run_id,))
        return ExpansionRun.from_row(row) if row else None

    def get_latest_for_task(self, task_id: str) -> ExpansionRun | None:
        """Get the most recent expansion run for a task."""
        row = self.db.fetchone(
            """
            SELECT * FROM expansion_runs
            WHERE parent_task_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (task_id,),
        )
        return ExpansionRun.from_row(row) if row else None

    def get_active_for_task(self, task_id: str) -> ExpansionRun | None:
        """Get the most recent non-terminal expansion run for a task."""
        placeholders = ", ".join("?" for _ in self._ACTIVE_STATUSES)
        row = self.db.fetchone(
            f"""
            SELECT * FROM expansion_runs
            WHERE parent_task_id = ?
              AND status IN ({placeholders})
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (task_id, *self._ACTIVE_STATUSES),
        )
        return ExpansionRun.from_row(row) if row else None

    def list_for_task(
        self,
        task_id: str,
        *,
        statuses: list[ExpansionRunStatus] | None = None,
        limit: int = 20,
    ) -> list[ExpansionRun]:
        """List expansion runs for a task."""
        query = "SELECT * FROM expansion_runs WHERE parent_task_id = ?"
        params: list[Any] = [task_id]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [ExpansionRun.from_row(row) for row in self.db.fetchall(query, tuple(params))]

    def start(self, run_id: str) -> ExpansionRun | None:
        """Mark a run as running."""
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            UPDATE expansion_runs
            SET status = 'running', started_at = COALESCE(started_at, ?), updated_at = ?, error = NULL
            WHERE id = ?
            """,
            (now, now, run_id),
        )
        return self.get(run_id)

    def save_compiled_spec(
        self,
        run_id: str,
        compiled_spec: dict[str, Any],
        *,
        checkpoints: dict[str, Any] | None = None,
    ) -> ExpansionRun | None:
        """Persist a compiled expansion spec and mark the run compiled."""
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            UPDATE expansion_runs
            SET status = 'compiled',
                compiled_spec_json = ?,
                checkpoints_json = COALESCE(?, checkpoints_json),
                updated_at = ?
            WHERE id = ?
            """,
            (
                json.dumps(compiled_spec),
                json.dumps(checkpoints) if checkpoints is not None else None,
                now,
                run_id,
            ),
        )
        return self.get(run_id)

    def mark_applying(self, run_id: str) -> ExpansionRun | None:
        """Mark a run as applying."""
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            "UPDATE expansion_runs SET status = 'applying', updated_at = ? WHERE id = ?",
            (now, run_id),
        )
        return self.get(run_id)

    def save_apply_result(
        self,
        run_id: str,
        *,
        task_id_map: dict[str, str],
        created_task_ids: list[str],
        checkpoints: dict[str, Any] | None = None,
        completed: bool = True,
    ) -> ExpansionRun | None:
        """Persist apply results and optionally mark the run completed."""
        now = datetime.now(UTC).isoformat()
        status: ExpansionRunStatus = "completed" if completed else "applying"
        self.db.execute(
            """
            UPDATE expansion_runs
            SET status = ?,
                task_id_map_json = ?,
                created_task_ids_json = ?,
                checkpoints_json = COALESCE(?, checkpoints_json),
                completed_at = CASE WHEN ? THEN ? ELSE completed_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status,
                json.dumps(task_id_map),
                json.dumps(created_task_ids),
                json.dumps(checkpoints) if checkpoints is not None else None,
                completed,
                now,
                now,
                run_id,
            ),
        )
        return self.get(run_id)

    def save_qa_result(self, run_id: str, qa_result: dict[str, Any]) -> ExpansionRun | None:
        """Persist QA output for a run."""
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            UPDATE expansion_runs
            SET qa_result_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(qa_result), now, run_id),
        )
        return self.get(run_id)

    def append_log(
        self,
        run_id: str,
        *,
        level: str,
        message: str,
        extra: dict[str, Any] | None = None,
    ) -> ExpansionRun | None:
        """Append a structured log entry to a run."""
        now = datetime.now(UTC).isoformat()
        entry = {
            "timestamp": now,
            "level": level,
            "message": message,
            "extra": extra or {},
        }
        cursor = self.db.execute(
            """
            UPDATE expansion_runs
            SET logs_json = COALESCE(logs_json, '[]'::jsonb) || ?::jsonb,
                updated_at = ?
            WHERE id = ?
            """,
            (json.dumps([entry]), now, run_id),
        )
        if cursor.rowcount == 0:
            return None
        return self.get(run_id)

    def update_checkpoints(
        self,
        run_id: str,
        checkpoints: dict[str, Any],
    ) -> ExpansionRun | None:
        """Replace checkpoint metadata for a run."""
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            UPDATE expansion_runs
            SET checkpoints_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(checkpoints), now, run_id),
        )
        return self.get(run_id)

    def fail(self, run_id: str, error: str) -> ExpansionRun | None:
        """Mark a run failed."""
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            UPDATE expansion_runs
            SET status = 'failed', error = ?, completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (error, now, now, run_id),
        )
        logger.warning("Expansion run %s failed: %s", run_id, error)
        return self.get(run_id)

    def cancel(self, run_id: str, error: str | None = None) -> ExpansionRun | None:
        """Mark a run cancelled."""
        now = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            UPDATE expansion_runs
            SET status = 'cancelled', error = ?, completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (error, now, now, run_id),
        )
        return self.get(run_id)
