"""Build run and event history storage."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.id import generate_prefixed_id

logger = logging.getLogger(__name__)

BuildRunStatus = Literal["started", "completed", "failed", "skipped"]


@dataclass(frozen=True)
class BuildRun:
    id: str
    project_id: str
    root_task_id: str | None
    input_ref: str | None
    action: str
    status: str
    actor: str
    summary: dict[str, Any] | None
    error: str | None
    started_at: str
    completed_at: str | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> BuildRun:
        return cls(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            root_task_id=_optional_str(row["root_task_id"]),
            input_ref=_optional_str(row["input_ref"]),
            action=str(row["action"]),
            status=str(row["status"]),
            actor=str(row["actor"]),
            summary=_json_obj(row["summary_json"]),
            error=_optional_str(row["error"]),
            started_at=str(row["started_at"]),
            completed_at=_optional_str(row["completed_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BuildHistoryEvent:
    id: int
    run_id: str | None
    project_id: str
    root_task_id: str | None
    task_id: str | None
    event_type: str
    action: str | None
    message: str | None
    payload: dict[str, Any] | None
    created_at: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> BuildHistoryEvent:
        return cls(
            id=int(row["id"]),
            run_id=_optional_str(row["run_id"]),
            project_id=str(row["project_id"]),
            root_task_id=_optional_str(row["root_task_id"]),
            task_id=_optional_str(row["task_id"]),
            event_type=str(row["event_type"]),
            action=_optional_str(row["action"]),
            message=_optional_str(row["message"]),
            payload=_json_obj(row["payload_json"]),
            created_at=str(row["created_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BuildHistoryStorage:
    """Append-only build observability storage."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def start_run(
        self,
        *,
        project_id: str,
        action: str,
        root_task_id: str | None = None,
        input_ref: str | None = None,
        actor: str = "build",
        summary: Mapping[str, Any] | None = None,
    ) -> BuildRun:
        run_id = generate_prefixed_id("br", length=12)
        now = _now()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO build_runs (
                    id, project_id, root_task_id, input_ref, action, status, actor,
                    summary_json, error, started_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, 'started', ?, ?, NULL, ?, NULL)
                """,
                (
                    run_id,
                    project_id,
                    root_task_id,
                    input_ref,
                    action,
                    actor,
                    _json_dump(summary),
                    now,
                ),
            )
        return self._require_run(run_id)

    def finish_run(
        self,
        run_id: str,
        *,
        status: BuildRunStatus,
        root_task_id: str | None = None,
        summary: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> BuildRun:
        now = _now()
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE build_runs
                   SET status = ?,
                       root_task_id = COALESCE(?, root_task_id),
                       summary_json = COALESCE(?, summary_json),
                       error = ?,
                       completed_at = ?
                 WHERE id = ?
                """,
                (status, root_task_id, _json_dump(summary), error, now, run_id),
            )
        return self._require_run(run_id)

    def update_run_context(
        self,
        run_id: str,
        *,
        root_task_id: str | None = None,
        summary: Mapping[str, Any] | None = None,
    ) -> BuildRun:
        current = self._require_run(run_id)
        merged_summary: dict[str, Any] | None = None
        if summary is not None:
            merged_summary = dict(current.summary or {})
            merged_summary.update(summary)
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE build_runs
                   SET root_task_id = COALESCE(?, root_task_id),
                       summary_json = COALESCE(?, summary_json)
                 WHERE id = ?
                """,
                (root_task_id, _json_dump(merged_summary), run_id),
            )
        return self._require_run(run_id)

    def record_run(
        self,
        *,
        project_id: str,
        action: str,
        status: BuildRunStatus = "completed",
        root_task_id: str | None = None,
        input_ref: str | None = None,
        actor: str = "build",
        summary: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> BuildRun:
        run_id = generate_prefixed_id("br", length=12)
        now = _now()
        completed_at = now if status != "started" else None
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO build_runs (
                    id, project_id, root_task_id, input_ref, action, status, actor,
                    summary_json, error, started_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    project_id,
                    root_task_id,
                    input_ref,
                    action,
                    status,
                    actor,
                    _json_dump(summary),
                    error,
                    now,
                    completed_at,
                ),
            )
        return self._require_run(run_id)

    def record_event(
        self,
        *,
        project_id: str,
        event_type: str,
        run_id: str | None = None,
        root_task_id: str | None = None,
        task_id: str | None = None,
        action: str | None = None,
        message: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> BuildHistoryEvent:
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                INSERT INTO build_history_events (
                    run_id, project_id, root_task_id, task_id, event_type,
                    action, message, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING id
                """,
                (
                    run_id,
                    project_id,
                    root_task_id,
                    task_id,
                    event_type,
                    action,
                    message,
                    _json_dump(payload),
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("Database did not return a build history event id")
        event = self.get_event(int(row["id"]))
        if event is None:
            raise RuntimeError("Build history event disappeared after insert")
        return event

    def get_run(self, run_id: str) -> BuildRun | None:
        row = self.db.fetchone("SELECT * FROM build_runs WHERE id = ?", (run_id,))
        return BuildRun.from_row(row) if row is not None else None

    def _require_run(self, run_id: str) -> BuildRun:
        run = self.get_run(run_id)
        if run is None:
            raise RuntimeError(f"Build run disappeared after insert/update: {run_id}")
        return run

    def get_event(self, event_id: int) -> BuildHistoryEvent | None:
        row = self.db.fetchone("SELECT * FROM build_history_events WHERE id = ?", (event_id,))
        return BuildHistoryEvent.from_row(row) if row is not None else None

    def latest_run_for_input(self, project_id: str, input_ref: str) -> BuildRun | None:
        row = self.db.fetchone(
            """
            SELECT *
              FROM build_runs
             WHERE project_id = ?
               AND input_ref = ?
             ORDER BY started_at DESC, id DESC
             LIMIT 1
            """,
            (project_id, input_ref),
        )
        return BuildRun.from_row(row) if row is not None else None

    def latest_coordinated_run_for_task(self, project_id: str, task_id: str) -> BuildRun | None:
        ancestor_rows = self.db.fetchall(
            """
            WITH RECURSIVE ancestors(id, parent_task_id) AS (
                SELECT id, parent_task_id
                  FROM tasks
                 WHERE id = ? AND project_id = ?
                UNION ALL
                SELECT parent.id, parent.parent_task_id
                  FROM tasks parent
                  JOIN ancestors child ON child.parent_task_id = parent.id
                 WHERE parent.project_id = ?
            )
            SELECT id FROM ancestors
            """,
            (task_id, project_id, project_id),
        )
        ancestor_ids = [str(row["id"]) for row in ancestor_rows]
        if not ancestor_ids:
            return None
        placeholders = ", ".join("?" for _ in ancestor_ids)
        rows = self.db.fetchall(
            f"""
            SELECT *
              FROM build_runs
             WHERE project_id = ?
               AND root_task_id IN ({placeholders})
             ORDER BY started_at DESC, id DESC
             LIMIT 100
            """,  # nosec B608 # placeholders are generated from trusted list length only.
            (project_id, *ancestor_ids),
        )
        for row in rows:
            run = BuildRun.from_row(row)
            if run.summary and run.summary.get("coordinator_session_id"):
                return run
        return None

    def list_runs(
        self,
        *,
        project_id: str,
        root_task_id: str | None = None,
        input_ref: str | None = None,
        limit: int = 20,
    ) -> list[BuildRun]:
        where = ["project_id = ?"]
        params: list[Any] = [project_id]
        if root_task_id is not None:
            where.append("root_task_id = ?")
            params.append(root_task_id)
        elif input_ref is not None:
            where.append("input_ref = ?")
            params.append(input_ref)
        params.append(_limit(limit))
        rows = self.db.fetchall(
            f"""
            SELECT *
              FROM build_runs
             WHERE {" AND ".join(where)}
             ORDER BY started_at DESC, id DESC
             LIMIT ?
            """,  # nosec B608 # where clauses are fixed strings.
            tuple(params),
        )
        return [BuildRun.from_row(row) for row in rows]

    def list_events(
        self,
        *,
        project_id: str,
        root_task_id: str | None = None,
        input_ref: str | None = None,
        limit: int = 20,
    ) -> list[BuildHistoryEvent]:
        params: list[Any] = [project_id]
        if root_task_id is not None:
            where = "e.project_id = ? AND e.root_task_id = ?"
            params.append(root_task_id)
        elif input_ref is not None:
            where = "e.project_id = ? AND r.input_ref = ?"
            params.append(input_ref)
        else:
            where = "e.project_id = ?"
        params.append(_limit(limit))
        rows = self.db.fetchall(
            f"""
            SELECT e.*
              FROM build_history_events e
              LEFT JOIN build_runs r ON r.id = e.run_id
             WHERE {where}
             ORDER BY e.created_at DESC, e.id DESC
             LIMIT ?
            """,  # nosec B608 # where clause is selected from fixed templates.
            tuple(params),
        )
        return [BuildHistoryEvent.from_row(row) for row in rows]


def best_effort_start_run(db: HubDatabase, **kwargs: Any) -> BuildRun | None:
    try:
        return BuildHistoryStorage(db).start_run(**kwargs)
    except Exception:
        logger.warning("Failed to record build run start", exc_info=True)
        return None


def best_effort_finish_run(db: HubDatabase, run_id: str | None, **kwargs: Any) -> BuildRun | None:
    if run_id is None:
        return None
    try:
        return BuildHistoryStorage(db).finish_run(run_id, **kwargs)
    except Exception:
        logger.warning("Failed to record build run finish", exc_info=True)
        return None


def best_effort_update_run_context(
    db: HubDatabase, run_id: str | None, **kwargs: Any
) -> BuildRun | None:
    if run_id is None:
        return None
    try:
        return BuildHistoryStorage(db).update_run_context(run_id, **kwargs)
    except Exception:
        logger.warning("Failed to update build run context", exc_info=True)
        return None


def best_effort_record_run(db: HubDatabase, **kwargs: Any) -> BuildRun | None:
    try:
        return BuildHistoryStorage(db).record_run(**kwargs)
    except Exception:
        logger.warning("Failed to record build run", exc_info=True)
        return None


def best_effort_record_event(db: HubDatabase, **kwargs: Any) -> BuildHistoryEvent | None:
    try:
        return BuildHistoryStorage(db).record_event(**kwargs)
    except Exception:
        logger.warning("Failed to record build history event", exc_info=True)
        return None


def _json_dump(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))


def _json_obj(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value:
        return None
    loaded = json.loads(value)
    return loaded if isinstance(loaded, dict) else None


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _limit(value: int) -> int:
    return max(1, min(int(value), 100))


__all__ = [
    "BuildHistoryEvent",
    "BuildHistoryStorage",
    "BuildRun",
    "best_effort_finish_run",
    "best_effort_record_event",
    "best_effort_record_run",
    "best_effort_start_run",
    "best_effort_update_run_context",
]
