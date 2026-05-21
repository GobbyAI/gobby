"""Write-set overlap guard for lifecycle dispatch."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from gobby.dispatch.actions import Action, SpawnAgentAction, StartStageAction
from gobby.storage.database import DatabaseProtocol


@dataclass(frozen=True)
class WriteSetOverlap:
    """Details for a candidate blocked by another task's write set."""

    task_id: str
    blocking_task_ids: tuple[str, ...]
    file_paths: tuple[str, ...]


class DispatchWriteSetGuard:
    """Tracks file write sets already active for a dispatcher heartbeat."""

    def __init__(
        self,
        *,
        db: DatabaseProtocol,
        file_owners: dict[str, set[str]] | None = None,
    ) -> None:
        self._db = db
        self._file_owners = file_owners or {}
        self._task_files: dict[str, frozenset[str]] = {}

    @classmethod
    def load(
        cls,
        db: DatabaseProtocol,
        *,
        project_id: str | None = None,
    ) -> DispatchWriteSetGuard:
        """Build a guard from active write-set owners already in storage."""
        owners: dict[str, set[str]] = defaultdict(set)
        now = datetime.now(UTC).isoformat()
        project_filter = ""
        params: list[object] = [now]
        if project_id is not None:
            project_filter = "AND t.project_id = ?"
            params.append(project_id)

        rows = db.fetchall(
            f"""
            WITH current_stage AS (
                SELECT s.*
                  FROM task_stage_states s
                  JOIN (
                        SELECT task_id, MIN(position) AS position
                          FROM task_stage_states
                         WHERE state != 'done'
                         GROUP BY task_id
                  ) current_position
                    ON current_position.task_id = s.task_id
                   AND current_position.position = s.position
            )
            SELECT DISTINCT taf.task_id, taf.file_path
              FROM task_affected_files taf
              JOIN tasks t ON t.id = taf.task_id
              LEFT JOIN current_stage cs ON cs.task_id = t.id
              LEFT JOIN agent_runs ar
                ON ar.task_id = t.id
               AND ar.status IN ('pending', 'running')
              LEFT JOIN task_dispatch_mutex mutex
                ON mutex.task_id = t.id
               AND mutex.run_id IS NOT NULL
               AND (
                    mutex.lease_until IS NULL
                    OR mutex.lease_until >= ?
               )
             WHERE t.closed_at IS NULL
               AND t.escalated_at IS NULL
               AND COALESCE(t.is_escalated, 0) = 0
               {project_filter}
               AND (
                    t.claimed_by_session_id IS NOT NULL
                    OR ar.id IS NOT NULL
                    OR mutex.task_id IS NOT NULL
                    OR (
                        cs.stage_name = 'development'
                        AND cs.state IN ('in_progress', 'needs_review', 'review_approved')
                    )
                    OR (
                        cs.stage_name = 'merge'
                        AND cs.state IN (
                            'ready',
                            'in_progress',
                            'needs_review',
                            'review_approved'
                        )
                    )
               )
            """,  # nosec B608 # project_filter is a static optional predicate.
            tuple(params),
        )
        for row in rows:
            file_path = _normalized_path(row["file_path"])
            if file_path:
                owners[file_path].add(str(row["task_id"]))
        return cls(db=db, file_owners=dict(owners))

    def action_reserves_write_set(self, action: Action, task: object) -> bool:
        """Return whether executing action starts or continues write-set work."""
        if isinstance(action, StartStageAction):
            return action.stage_name == "development"
        if isinstance(action, SpawnAgentAction):
            stage = _current_stage(task)
            return _stage_name(stage) == "development" and _stage_state(stage) == "in_progress"
        return False

    def conflict_for(self, task_id: str) -> WriteSetOverlap | None:
        """Return an overlap when task_id's write set is already owned by another task."""
        files = self._files_for_task(task_id)
        if not files:
            return None

        blockers: set[str] = set()
        overlapping_files: list[str] = []
        for file_path in sorted(files):
            owners = self._file_owners.get(file_path, set()) - {task_id}
            if not owners:
                continue
            blockers.update(owners)
            overlapping_files.append(file_path)

        if not blockers:
            return None
        return WriteSetOverlap(
            task_id=task_id,
            blocking_task_ids=tuple(sorted(blockers)),
            file_paths=tuple(overlapping_files),
        )

    def reserve(self, task_id: str) -> None:
        """Mark a successfully dispatched task's files as occupied in this heartbeat."""
        for file_path in self._files_for_task(task_id):
            self._file_owners.setdefault(file_path, set()).add(task_id)

    def _files_for_task(self, task_id: str) -> frozenset[str]:
        cached = self._task_files.get(task_id)
        if cached is not None:
            return cached
        rows = self._db.fetchall(
            """
            SELECT DISTINCT file_path
              FROM task_affected_files
             WHERE task_id = ?
             ORDER BY file_path
            """,
            (task_id,),
        )
        files = frozenset(
            file_path for row in rows if (file_path := _normalized_path(row["file_path"]))
        )
        self._task_files[task_id] = files
        return files


def _current_stage(task: object) -> object | None:
    current_stage = cast(object | None, _field(task, "current_stage"))
    if current_stage is not None:
        return current_stage
    stages = [
        cast(object, stage)
        for stage in tuple(_field(task, "stages", ()) or ())
        if _stage_state(stage) != "done"
    ]
    if not stages:
        return None
    return min(stages, key=lambda stage: int(_field(stage, "position", 0) or 0))


def _stage_name(stage: object | None) -> str:
    if stage is None:
        return ""
    return str(_field(stage, "stage_name", _field(stage, "name", "")))


def _stage_state(stage: object | None) -> str:
    if stage is None:
        return ""
    return str(_field(stage, "state", ""))


def _field(obj: object | None, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _normalized_path(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


__all__ = ["DispatchWriteSetGuard", "WriteSetOverlap"]
