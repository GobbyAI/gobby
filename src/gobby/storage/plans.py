"""DB-backed plan registry and managed coverage-manifest lifecycle."""

from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from gobby.plans.coverage import evaluate
from gobby.plans.coverage_manifest import coverage_manifest_path, write_manifest
from gobby.plans.parser import PlanKind, parse_plan
from gobby.storage.database import DatabaseProtocol
from gobby.storage.projects import LocalProjectManager

PlanState = Literal["active", "archived"]
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PlanRecord:
    id: str
    project_id: str
    plan_id: str
    plan_path: str
    plan_hash: str | None
    plan_kind: str
    state: PlanState
    root_task_ref: str
    created_at: str
    updated_at: str
    archived_at: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> PlanRecord:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            plan_id=row["plan_id"],
            plan_path=row["plan_path"],
            plan_hash=row["plan_hash"],
            plan_kind=row["plan_kind"],
            state=row["state"],
            root_task_ref=row["root_task_ref"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived_at=row["archived_at"],
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class PlanNotFoundError(ValueError):
    """Raised when a plan row cannot be resolved."""


class LocalPlanManager:
    """CRUD wrapper for the DB-backed plan index."""

    def __init__(self, db: DatabaseProtocol):
        self.db = db

    def create_plan(
        self,
        *,
        project_id: str,
        plan_id: str,
        plan_path: str | Path,
        plan_kind: str = PlanKind.implementation.value,
        root_task_ref: str,
    ) -> PlanRecord:
        project_root = self._project_root(project_id)
        relative_path = self._relative_plan_path(project_root, plan_path)
        doc = parse_plan(
            project_root / relative_path, plan_kind=PlanKind(plan_kind), parse_mode="draft"
        )
        now = _now()
        record_id = str(uuid.uuid4())

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO plans (
                    id, project_id, plan_id, plan_path, plan_hash, plan_kind, state,
                    root_task_ref, created_at, updated_at, archived_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, NULL)
                ON CONFLICT(project_id, plan_id) DO UPDATE SET
                    plan_path = excluded.plan_path,
                    plan_hash = excluded.plan_hash,
                    plan_kind = excluded.plan_kind,
                    state = 'active',
                    root_task_ref = excluded.root_task_ref,
                    updated_at = excluded.updated_at,
                    archived_at = NULL
                """,
                (
                    record_id,
                    project_id,
                    plan_id,
                    str(relative_path),
                    doc.source_hash,
                    plan_kind,
                    root_task_ref,
                    now,
                    now,
                ),
            )

        record = self.get_plan(plan_id, project_id=project_id)
        self.regenerate_coverage_manifest(record.plan_id, project_id=record.project_id)
        return record

    def get_plan(self, plan_id_or_ref: str, *, project_id: str | None = None) -> PlanRecord:
        row = self._find_plan(plan_id_or_ref, project_id=project_id)
        if row is None:
            raise PlanNotFoundError(f"plan not found: {plan_id_or_ref}")
        return PlanRecord.from_row(row)

    def list_plans(
        self,
        *,
        state: str | None = None,
        plan_kind: str | None = None,
        project_id: str | None = None,
    ) -> list[PlanRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state)
        if plan_kind is not None:
            clauses.append("plan_kind = ?")
            params.append(plan_kind)
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.fetchall(
            f"""
            SELECT * FROM plans
            {where}
            ORDER BY updated_at DESC, plan_id ASC
            """,  # nosec B608 - WHERE clause is assembled from fixed fragments.
            tuple(params),
        )
        return [PlanRecord.from_row(row) for row in rows]

    def update_plan_hash(self, plan_id: str, *, project_id: str | None = None) -> PlanRecord:
        record = self.get_plan(plan_id, project_id=project_id)
        project_root = self._project_root(record.project_id)
        doc = parse_plan(
            project_root / record.plan_path,
            plan_kind=PlanKind(record.plan_kind),
            parse_mode="draft",
        )
        if doc.source_hash == record.plan_hash:
            return record

        now = _now()
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE plans
                SET plan_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (doc.source_hash, now, record.id),
            )
        updated = self.get_plan(plan_id, project_id=record.project_id)
        self.regenerate_coverage_manifest(updated.plan_id, project_id=updated.project_id)
        return updated

    def regenerate_coverage_manifest(
        self,
        plan_id: str,
        *,
        project_id: str | None = None,
    ) -> Path:
        record = self.get_plan(plan_id, project_id=project_id)
        project_root = self._project_root(record.project_id)
        report = evaluate(
            plan=project_root / record.plan_path,
            plan_id=record.plan_id,
            plan_hash=record.plan_hash or "",
            task_tree="db",
            root_task_ref=record.root_task_ref,
            project_id=record.project_id,
            db=self.db,
        )
        return write_manifest(report, project_root, regenerate=True)

    def archive_plan(
        self,
        plan_id: str,
        *,
        project_id: str | None = None,
        reason: str | None = None,
    ) -> PlanRecord:
        record = self.get_plan(plan_id, project_id=project_id)
        if record.state == "archived":
            return record

        project_root = self._project_root(record.project_id)
        source_path = project_root / record.plan_path
        completed_path = project_root / ".gobby" / "plans" / "completed" / source_path.name
        previous_path = source_path
        moved = False
        if source_path.exists():
            completed_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_path), str(completed_path))
            moved = True

        archived_at = _now()
        archived_relative = completed_path.relative_to(project_root)
        try:
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    UPDATE plans
                    SET state = 'archived',
                        plan_path = ?,
                        archived_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (str(archived_relative), archived_at, archived_at, record.id),
                )
        except Exception as exc:
            if moved and completed_path.exists():
                previous_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(completed_path), str(previous_path))
                except Exception:
                    logger.error(
                        "Failed to roll back archived plan file move",
                        exc_info=True,
                        extra={
                            "plan_id": record.plan_id,
                            "moved": moved,
                            "completed_path": str(completed_path),
                            "previous_path": str(previous_path),
                            "original_error": str(exc),
                        },
                    )
            raise

        self._remove_coverage_manifest(record)
        return self.get_plan(plan_id, project_id=record.project_id)

    def delete_plan(self, plan_id: str, *, project_id: str | None = None) -> bool:
        record = self.get_plan(plan_id, project_id=project_id)
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM plans WHERE id = ?", (record.id,))
            deleted_count = cursor.rowcount
        self._remove_coverage_manifest(record)
        return deleted_count > 0

    def _find_plan(self, plan_id_or_ref: str, *, project_id: str | None) -> Any | None:
        params: list[object] = [plan_id_or_ref, _normalize_ref(plan_id_or_ref)]
        project_clause = ""
        if project_id is not None:
            project_clause = "AND project_id = ?"
            params.append(project_id)
        return self.db.fetchone(
            f"""
            SELECT * FROM plans
            WHERE (plan_id = ? OR root_task_ref = ?)
            {project_clause}
            ORDER BY updated_at DESC
            LIMIT 1
            """,  # nosec B608 - project_clause is fixed text.
            tuple(params),
        )

    def _project_root(self, project_id: str) -> Path:
        project = LocalProjectManager(self.db).get(project_id)
        if project is None or not project.repo_path:
            raise ValueError(f"project {project_id!r} has no repo_path")
        return Path(project.repo_path)

    def _relative_plan_path(self, project_root: Path, plan_path: str | Path) -> Path:
        path = Path(plan_path)
        absolute = path if path.is_absolute() else project_root / path
        if not absolute.exists():
            raise FileNotFoundError(f"plan file not found: {absolute}")
        try:
            return absolute.relative_to(project_root)
        except ValueError as exc:
            raise ValueError(f"plan file must be inside project root: {absolute}") from exc

    def _remove_coverage_manifest(self, record: PlanRecord) -> None:
        path = coverage_manifest_path(
            self._project_root(record.project_id),
            project_id=record.project_id,
            root_task_ref=record.root_task_ref,
            plan_id=record.plan_id,
        )
        path.unlink(missing_ok=True)


def _normalize_ref(ref: str) -> str:
    stripped = ref.strip()
    if not stripped:
        raise ValueError("plan ref must not be blank")
    if stripped.isdecimal():
        return f"#{stripped}"
    return stripped


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


__all__ = ["LocalPlanManager", "PlanNotFoundError", "PlanRecord", "PlanState"]
