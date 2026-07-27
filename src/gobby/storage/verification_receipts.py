"""Durable project/session/task-scoped verification receipts."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager as LocalSessionManager
from gobby.utils.datetime import utc_now

VerificationOutcome = Literal["pending", "success", "failure", "unknown", "conflicting"]
AttributionSource = Literal[
    "active_task",
    "sole_claim",
    "explicit_task",
    "worktree_task",
    "manual_assignment",
    "unassigned",
]

_OUTPUT_EXCERPT_BYTES = 4096


def verification_receipt_id(
    project_id: str,
    session_id: str,
    provider: str,
    execution_id: str,
) -> str:
    """Return the stable ID for one provider execution identity."""
    key = f"gobby:verification-receipt:{project_id}:{session_id}:{provider}:{execution_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


def _bounded_output(output: str | None) -> tuple[str | None, str | None, str | None, int | None]:
    if output is None:
        return None, None, None, None
    encoded = output.encode("utf-8")
    first = (
        encoded[:_OUTPUT_EXCERPT_BYTES].decode("utf-8", errors="replace").replace("\x00", "\ufffd")
    )
    last = (
        encoded[-_OUTPUT_EXCERPT_BYTES:].decode("utf-8", errors="replace").replace("\x00", "\ufffd")
    )
    return first, last, hashlib.sha256(encoded).hexdigest(), len(encoded)


@dataclass(frozen=True)
class VerificationReceipt:
    """One durable command or manual verification result."""

    id: str
    project_id: str
    session_id: str
    task_id: str | None
    provider: str
    execution_id: str
    source_event_id: str
    evidence_type: str
    command: str | None
    cwd: str | None
    normalized_outcome: VerificationOutcome
    outcome_provenance: str | None
    exit_code: int | None
    started_at: datetime
    completed_at: datetime | None
    output_first_4k: str | None
    output_last_4k: str | None
    output_sha256: str | None
    output_bytes: int | None
    details: dict[str, Any]
    attribution_source: AttributionSource
    attribution_actor: str | None
    attributed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    validation_epoch: int | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> VerificationReceipt:
        details = row.get("details") or {}
        if isinstance(details, str):
            details = json.loads(details)
        return cls(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            session_id=str(row["session_id"]),
            task_id=str(row["task_id"]) if row.get("task_id") else None,
            provider=str(row["provider"]),
            execution_id=str(row["execution_id"]),
            source_event_id=str(row["source_event_id"]),
            evidence_type=str(row["evidence_type"]),
            command=str(row["command"]) if row.get("command") is not None else None,
            cwd=str(row["cwd"]) if row.get("cwd") is not None else None,
            normalized_outcome=row["normalized_outcome"],
            outcome_provenance=(
                str(row["outcome_provenance"])
                if row.get("outcome_provenance") is not None
                else None
            ),
            exit_code=int(row["exit_code"]) if row.get("exit_code") is not None else None,
            started_at=row["started_at"],
            completed_at=row.get("completed_at"),
            output_first_4k=row.get("output_first_4k"),
            output_last_4k=row.get("output_last_4k"),
            output_sha256=row.get("output_sha256"),
            output_bytes=(
                int(row["output_bytes"]) if row.get("output_bytes") is not None else None
            ),
            validation_epoch=(
                int(row["validation_epoch"]) if row.get("validation_epoch") is not None else None
            ),
            details=dict(details),
            attribution_source=row["attribution_source"],
            attribution_actor=row.get("attribution_actor"),
            attributed_at=row.get("attributed_at"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "provider": self.provider,
            "execution_id": self.execution_id,
            "source_event_id": self.source_event_id,
            "evidence_type": self.evidence_type,
            "command": self.command,
            "cwd": self.cwd,
            "normalized_outcome": self.normalized_outcome,
            "outcome_provenance": self.outcome_provenance,
            "exit_code": self.exit_code,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "output_first_4k": self.output_first_4k,
            "output_last_4k": self.output_last_4k,
            "output_sha256": self.output_sha256,
            "output_bytes": self.output_bytes,
            "validation_epoch": self.validation_epoch,
            "details": self.details,
            "attribution_source": self.attribution_source,
            "attribution_actor": self.attribution_actor,
            "attributed_at": self.attributed_at.isoformat() if self.attributed_at else None,
        }


@dataclass(frozen=True)
class VerificationReceiptWrite:
    project_id: str
    session_id: str
    provider: str
    execution_id: str
    source_event_id: str
    evidence_type: str
    normalized_outcome: VerificationOutcome
    started_at: datetime
    task_id: str | None = None
    command: str | None = None
    cwd: str | None = None
    outcome_provenance: str | None = None
    exit_code: int | None = None
    completed_at: datetime | None = None
    output: str | None = None
    validation_epoch: int | None = None
    details: Mapping[str, Any] = field(default_factory=dict)
    attribution_source: AttributionSource = "unassigned"
    attribution_actor: str | None = None
    attributed_at: datetime | None = None


class VerificationReceiptStore:
    """Persistence and attribution operations for verification receipts."""

    def __init__(self, db: HubDatabase):
        self.db = db

    def resolve_task_ref(self, project_id: str, task_ref: str | None) -> str | None:
        if not task_ref:
            return None
        value = task_ref.strip()
        if value.startswith("#") and value[1:].isdigit():
            row = self.db.fetchone(
                "SELECT id FROM tasks WHERE project_id = %s AND seq_num = %s",
                (project_id, int(value[1:])),
            )
        elif "." in value and all(part.isdigit() for part in value.split(".")):
            row = self.db.fetchone(
                "SELECT id FROM tasks WHERE project_id = %s AND path_cache = %s",
                (project_id, value),
            )
        else:
            try:
                uuid.UUID(value)
            except ValueError:
                return None
            row = self.db.fetchone(
                "SELECT id FROM tasks WHERE project_id = %s AND id = %s",
                (project_id, value),
            )
        return str(row["id"]) if row else None

    def resolve_attribution(
        self,
        *,
        project_id: str,
        session_id: str,
        active_task_ref: str | None = None,
        explicit_task_ref: str | None = None,
        execution_cwd: str | None = None,
        session_task_ref: str | None = None,
    ) -> tuple[str | None, AttributionSource]:
        explicit_task_id = self.resolve_task_ref(project_id, explicit_task_ref)
        if explicit_task_id and self._is_open_claim(
            explicit_task_id,
            project_id,
            session_id,
            allow_owner_ancestor=True,
        ):
            return explicit_task_id, "explicit_task"

        worktree_task_id = self._resolve_worktree_task(
            project_id=project_id,
            session_id=session_id,
            execution_cwd=execution_cwd,
        )
        if worktree_task_id is not None:
            return worktree_task_id, "worktree_task"

        session_task_id = self.resolve_task_ref(project_id, session_task_ref)
        if session_task_id and self._is_open_claim(
            session_task_id,
            project_id,
            session_id,
            allow_owner_ancestor=True,
        ):
            return session_task_id, "active_task"

        active_task_id = self.resolve_task_ref(project_id, active_task_ref)
        if active_task_id and self._is_open_claim(
            active_task_id,
            project_id,
            session_id,
        ):
            return active_task_id, "active_task"

        rows = self.db.fetchall(
            """
            SELECT id FROM tasks
            WHERE project_id = %s
              AND claimed_by_session_id = %s
              AND closed_at IS NULL
            ORDER BY id
            LIMIT 2
            """,
            (project_id, session_id),
        )
        if len(rows) == 1:
            return str(rows[0]["id"]), "sole_claim"
        return None, "unassigned"

    def _resolve_worktree_task(
        self,
        *,
        project_id: str,
        session_id: str,
        execution_cwd: str | None,
    ) -> str | None:
        if execution_cwd is None:
            return None
        try:
            cwd = Path(execution_cwd)
            if not cwd.is_absolute():
                return None
            resolved_cwd = cwd.resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

        rows = self.db.fetchall(
            """
            SELECT task_id, worktree_path
            FROM worktrees
            WHERE project_id = %s AND task_id IS NOT NULL
            """,
            (project_id,),
        )
        matches: list[str] = []
        for row in rows:
            try:
                worktree_path = Path(str(row["worktree_path"]))
                if not worktree_path.is_absolute():
                    continue
                resolved_worktree = worktree_path.resolve(strict=False)
            except (OSError, RuntimeError, TypeError, ValueError):
                continue
            if resolved_cwd == resolved_worktree or resolved_cwd.is_relative_to(resolved_worktree):
                matches.append(str(row["task_id"]))

        if len(matches) != 1:
            return None
        task_id = matches[0]
        if self._is_open_claim(
            task_id,
            project_id,
            session_id,
            allow_owner_ancestor=True,
        ):
            return task_id
        return None

    def _is_open_claim(
        self,
        task_id: str,
        project_id: str,
        session_id: str,
        *,
        allow_owner_ancestor: bool = False,
    ) -> bool:
        row = self.db.fetchone(
            """
            SELECT claimed_by_session_id FROM tasks
            WHERE id = %s AND project_id = %s AND closed_at IS NULL
            """,
            (task_id, project_id),
        )
        if row is None or row["claimed_by_session_id"] is None:
            return False
        owner_session_id = str(row["claimed_by_session_id"])
        if owner_session_id == session_id:
            return True
        if not allow_owner_ancestor:
            return False
        session_manager = LocalSessionManager(self.db)
        owner_session = session_manager.get(owner_session_id)
        executor_session = session_manager.get(session_id)
        if (
            owner_session is None
            or executor_session is None
            or owner_session.project_id != project_id
            or executor_session.project_id != project_id
        ):
            return False
        return session_manager.is_ancestor(owner_session_id, session_id)

    def upsert(self, write: VerificationReceiptWrite) -> VerificationReceipt:
        first, last, digest, output_bytes = _bounded_output(write.output)
        now = utc_now()
        row = self.db.fetchone(
            """
            INSERT INTO verification_receipts (
                id, project_id, session_id, task_id, provider, execution_id,
                source_event_id, evidence_type, command, cwd, normalized_outcome,
                outcome_provenance, exit_code, started_at, completed_at,
                output_first_4k, output_last_4k, output_sha256, output_bytes,
                validation_epoch, details, attribution_source, attribution_actor, attributed_at,
                created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s
            )
            ON CONFLICT (project_id, session_id, provider, execution_id)
            DO UPDATE SET
                task_id = COALESCE(verification_receipts.task_id, EXCLUDED.task_id),
                command = COALESCE(EXCLUDED.command, verification_receipts.command),
                cwd = COALESCE(EXCLUDED.cwd, verification_receipts.cwd),
                normalized_outcome = CASE
                    WHEN verification_receipts.completed_at IS NOT NULL
                         AND EXCLUDED.normalized_outcome IN ('unknown', 'pending')
                    THEN verification_receipts.normalized_outcome
                    WHEN verification_receipts.completed_at IS NOT NULL
                         AND EXCLUDED.completed_at IS NOT NULL
                         AND verification_receipts.normalized_outcome NOT IN ('unknown', 'pending')
                         AND EXCLUDED.normalized_outcome NOT IN ('unknown', 'pending')
                         AND verification_receipts.normalized_outcome <> EXCLUDED.normalized_outcome
                    THEN 'conflicting'
                    WHEN verification_receipts.completed_at IS NOT NULL
                         AND EXCLUDED.completed_at IS NULL
                    THEN verification_receipts.normalized_outcome
                    ELSE EXCLUDED.normalized_outcome
                END,
                outcome_provenance = COALESCE(
                    EXCLUDED.outcome_provenance, verification_receipts.outcome_provenance
                ),
                exit_code = COALESCE(EXCLUDED.exit_code, verification_receipts.exit_code),
                completed_at = COALESCE(EXCLUDED.completed_at, verification_receipts.completed_at),
                output_first_4k = COALESCE(
                    EXCLUDED.output_first_4k, verification_receipts.output_first_4k
                ),
                output_last_4k = COALESCE(
                    EXCLUDED.output_last_4k, verification_receipts.output_last_4k
                ),
                output_sha256 = COALESCE(EXCLUDED.output_sha256, verification_receipts.output_sha256),
                output_bytes = COALESCE(EXCLUDED.output_bytes, verification_receipts.output_bytes),
                validation_epoch = COALESCE(
                    verification_receipts.validation_epoch, EXCLUDED.validation_epoch
                ),
                details = verification_receipts.details || EXCLUDED.details,
                attribution_source = CASE
                    WHEN verification_receipts.task_id IS NOT NULL
                    THEN verification_receipts.attribution_source
                    ELSE EXCLUDED.attribution_source
                END,
                attribution_actor = COALESCE(
                    verification_receipts.attribution_actor, EXCLUDED.attribution_actor
                ),
                attributed_at = COALESCE(
                    verification_receipts.attributed_at, EXCLUDED.attributed_at
                ),
                updated_at = EXCLUDED.updated_at
            RETURNING *
            """,
            (
                verification_receipt_id(
                    write.project_id,
                    write.session_id,
                    write.provider,
                    write.execution_id,
                ),
                write.project_id,
                write.session_id,
                write.task_id,
                write.provider,
                write.execution_id,
                write.source_event_id,
                write.evidence_type,
                write.command,
                write.cwd,
                write.normalized_outcome,
                write.outcome_provenance,
                write.exit_code,
                write.started_at,
                write.completed_at,
                first,
                last,
                digest,
                output_bytes,
                write.validation_epoch,
                json.dumps(dict(write.details), sort_keys=True),
                write.attribution_source,
                write.attribution_actor,
                write.attributed_at,
                now,
                now,
            ),
        )
        if row is None:
            raise RuntimeError("verification receipt upsert returned no row")
        return VerificationReceipt.from_row(row)

    def list_for_task(self, project_id: str, task_id: str) -> list[VerificationReceipt]:
        rows = self.db.fetchall(
            """
            SELECT * FROM verification_receipts
            WHERE project_id = %s AND task_id = %s
            ORDER BY COALESCE(completed_at, started_at) DESC, id DESC
            """,
            (project_id, task_id),
        )
        return [VerificationReceipt.from_row(row) for row in rows]

    def list_page(
        self,
        *,
        project_id: str,
        session_id: str,
        scope: Literal["task", "unassigned", "all"],
        task_id: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[VerificationReceipt], int]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        if scope == "task":
            if task_id is None:
                raise ValueError("task_id is required when scope='task'")
            where = "project_id = %s AND task_id = %s"
            params: list[Any] = [project_id, task_id]
        elif scope == "unassigned":
            where = "project_id = %s AND session_id = %s AND task_id IS NULL"
            params = [project_id, session_id]
        elif scope == "all":
            where = "project_id = %s AND session_id = %s"
            params = [project_id, session_id]
        else:
            raise ValueError("scope must be one of: task, unassigned, all")

        count_row = self.db.fetchone(
            f"SELECT COUNT(*) AS count FROM verification_receipts WHERE {where}",  # nosec B608
            tuple(params),
        )
        rows = self.db.fetchall(
            f"""SELECT * FROM verification_receipts WHERE {where}
                ORDER BY COALESCE(completed_at, started_at) DESC, id DESC
                LIMIT %s OFFSET %s""",  # nosec B608
            (*params, limit, offset),
        )
        if count_row is None:
            raise RuntimeError("verification receipt count query returned no row")
        return [VerificationReceipt.from_row(row) for row in rows], int(count_row["count"])

    def count_unassigned(self, project_id: str, session_id: str | None) -> int:
        if session_id is None:
            return 0
        row = self.db.fetchone(
            """
            SELECT COUNT(*) AS count FROM verification_receipts
            WHERE project_id = %s AND session_id = %s AND task_id IS NULL
            """,
            (project_id, session_id),
        )
        return int(row["count"]) if row else 0

    def has_success(self, project_id: str, session_id: str) -> bool:
        return (
            self.db.fetchone(
                """
                SELECT 1 FROM verification_receipts
                WHERE project_id = %s AND session_id = %s
                  AND normalized_outcome = 'success'
                LIMIT 1
                """,
                (project_id, session_id),
            )
            is not None
        )

    def assign_unassigned(
        self,
        *,
        project_id: str,
        session_id: str,
        task_id: str,
        receipt_ids: Sequence[str],
        actor: str,
    ) -> list[VerificationReceipt]:
        ids = list(dict.fromkeys(receipt_ids))
        if not ids:
            raise ValueError("receipt_ids must contain at least one ID")
        for receipt_id in ids:
            try:
                uuid.UUID(receipt_id)
            except ValueError as exc:
                raise ValueError(f"invalid receipt ID: {receipt_id}") from exc
        if not self._is_open_claim(task_id, project_id, session_id):
            raise ValueError("target task must be open and claimed by the current session")

        now = utc_now()
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT id, project_id, session_id, task_id
                FROM verification_receipts
                WHERE id = ANY(%s::uuid[])
                FOR UPDATE
                """,
                (ids,),
            ).fetchall()
            by_id = {str(row["id"]): row for row in rows}
            missing = [receipt_id for receipt_id in ids if receipt_id not in by_id]
            if missing:
                raise ValueError(f"verification receipt(s) not found: {', '.join(missing)}")
            for receipt_id in ids:
                row = by_id[receipt_id]
                if str(row["project_id"]) != project_id or str(row["session_id"]) != session_id:
                    raise ValueError("receipts must belong to the current project and session")
                if row["task_id"] is not None:
                    raise ValueError(f"verification receipt {receipt_id} is already assigned")

            conn.execute(
                """
                UPDATE verification_receipts
                SET task_id = %s, attribution_source = 'manual_assignment',
                    attribution_actor = %s, attributed_at = %s, updated_at = %s
                WHERE id = ANY(%s::uuid[])
                """,
                (task_id, actor, now, now, ids),
            )

        assigned_rows = self.db.fetchall(
            "SELECT * FROM verification_receipts WHERE id = ANY(%s::uuid[]) ORDER BY id",
            (ids,),
        )
        return [VerificationReceipt.from_row(row) for row in assigned_rows]
