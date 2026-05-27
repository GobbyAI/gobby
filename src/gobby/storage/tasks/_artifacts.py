"""Task artifact pointer storage helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from gobby.storage.hub.protocol import HubDatabase, Transaction

_ARTIFACT_FIELDS = frozenset(
    {
        "plan_file_path",
        "plan_file_hash",
        "worktree_path",
        "worktree_id",
        "clone_path",
        "clone_id",
        "base_commit_sha",
        "target_branch",
        "integration_branch",
        "integration_workspace_id",
        "integration_clone_id",
        "expansion_run_id",
        "expansion_attempts",
    }
)


class TaskArtifactConstraintError(ValueError):
    """Raised when artifact state violates an isolation-family invariant."""

    def __init__(self, predicate: str, message: str):
        self.predicate = predicate
        super().__init__(message)


ArtifactCheckConstraintError = TaskArtifactConstraintError


class MissingIsolationBaseError(TaskArtifactConstraintError):
    """Raised when a new isolation artifact write omits base_commit_sha."""

    def __init__(self) -> None:
        super().__init__(
            "missing_isolation_base",
            "base_commit_sha is required when setting worktree_path or clone_path",
        )


@dataclass(frozen=True)
class TaskArtifacts:
    task_id: str
    plan_file_path: str | None = None
    plan_file_hash: str | None = None
    worktree_path: str | None = None
    worktree_id: str | None = None
    clone_path: str | None = None
    clone_id: str | None = None
    base_commit_sha: str | None = None
    target_branch: str | None = None
    integration_branch: str | None = None
    integration_workspace_id: str | None = None
    integration_clone_id: str | None = None
    expansion_run_id: str | None = None
    expansion_attempts: int = 0
    updated_at: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> TaskArtifacts:
        return cls(
            task_id=row["task_id"],
            plan_file_path=row["plan_file_path"],
            plan_file_hash=row["plan_file_hash"],
            worktree_path=row["worktree_path"],
            worktree_id=row["worktree_id"],
            clone_path=row["clone_path"],
            clone_id=row["clone_id"],
            base_commit_sha=row["base_commit_sha"],
            target_branch=row["target_branch"],
            integration_branch=row["integration_branch"],
            integration_workspace_id=row["integration_workspace_id"],
            integration_clone_id=row["integration_clone_id"],
            expansion_run_id=row["expansion_run_id"],
            expansion_attempts=int(row["expansion_attempts"] or 0),
            updated_at=row["updated_at"],
        )


def _validate_field_names(fields: set[str]) -> None:
    unknown = fields - _ARTIFACT_FIELDS
    if unknown:
        allowed = ", ".join(sorted(_ARTIFACT_FIELDS))
        unknown_display = ", ".join(sorted(unknown))
        raise ValueError(f"Unknown task artifact field(s): {unknown_display}. Allowed: {allowed}.")


def _validate_constraints(values: dict[str, Any]) -> None:
    worktree_path_set = values.get("worktree_path") is not None
    worktree_id_set = values.get("worktree_id") is not None
    clone_path_set = values.get("clone_path") is not None
    clone_id_set = values.get("clone_id") is not None
    integration_workspace_set = values.get("integration_workspace_id") is not None
    integration_clone_set = values.get("integration_clone_id") is not None

    if worktree_path_set != worktree_id_set:
        raise TaskArtifactConstraintError(
            "worktree_pair",
            "worktree_path and worktree_id must be set or cleared together",
        )
    if clone_path_set != clone_id_set:
        raise TaskArtifactConstraintError(
            "clone_pair",
            "clone_path and clone_id must be set or cleared together",
        )
    if worktree_path_set and clone_path_set:
        raise TaskArtifactConstraintError(
            "isolation_family_xor",
            "worktree and clone artifact families are mutually exclusive",
        )
    if values.get("base_commit_sha") is not None and not worktree_path_set and not clone_path_set:
        raise TaskArtifactConstraintError(
            "isolation_base_without_family",
            "base_commit_sha requires an active worktree or clone artifact family",
        )
    if integration_workspace_set and integration_clone_set:
        raise TaskArtifactConstraintError(
            "integration_workspace_xor",
            "integration workspace and clone artifacts are mutually exclusive",
        )
    if (integration_workspace_set or integration_clone_set) and values.get(
        "integration_branch"
    ) is None:
        raise TaskArtifactConstraintError(
            "integration_id_without_branch",
            "integration_branch is required when setting an integration workspace id",
        )


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


_ISOLATION_FIELDS = frozenset({"worktree_path", "worktree_id", "clone_path", "clone_id"})


def _has_isolation_path(values: dict[str, Any]) -> bool:
    return values.get("worktree_path") is not None or values.get("clone_path") is not None


def _enforce_isolation_base(
    current: dict[str, Any],
    fields: dict[str, str | int | None],
    next_values: dict[str, Any],
) -> None:
    if not _has_isolation_path(next_values) or next_values.get("base_commit_sha") is not None:
        return

    row_exists = current.get("updated_at") is not None
    sets_new_isolation_path = any(
        fields.get(field) is not None for field in ("worktree_path", "clone_path")
    )
    modifies_isolation = any(
        field in fields and fields[field] != current.get(field) for field in _ISOLATION_FIELDS
    )

    if (not row_exists and sets_new_isolation_path) or (row_exists and modifies_isolation):
        raise MissingIsolationBaseError()


def _get_artifacts_in_transaction(conn: Transaction, task_id: str) -> TaskArtifacts:
    row = conn.execute("SELECT * FROM task_artifacts WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return TaskArtifacts(task_id=task_id)
    return TaskArtifacts.from_row(row)


def _set_artifacts_in_transaction(
    conn: Transaction,
    task_id: str,
    fields: dict[str, str | int | None],
) -> TaskArtifacts:
    _validate_field_names(set(fields))
    current = asdict(_get_artifacts_in_transaction(conn, task_id))
    next_values = {
        field: current[field] for field in _ARTIFACT_FIELDS if field != "expansion_attempts"
    }
    next_values["expansion_attempts"] = int(current["expansion_attempts"] or 0)
    next_values.update(fields)
    if next_values["expansion_attempts"] is None:
        next_values["expansion_attempts"] = 0
    next_values["expansion_attempts"] = int(next_values["expansion_attempts"])
    _validate_constraints(next_values)
    _enforce_isolation_base(current, fields, next_values)

    columns = sorted(_ARTIFACT_FIELDS)
    placeholders = ", ".join("?" for _ in columns)
    update_clause = ", ".join(f"{column} = excluded.{column}" for column in columns)
    params = [task_id, *(next_values[column] for column in columns)]

    conn.execute(
        f"""
        INSERT INTO task_artifacts (
            task_id, {", ".join(columns)}, updated_at
        )
        VALUES (?, {placeholders}, CURRENT_TIMESTAMP)
        ON CONFLICT(task_id) DO UPDATE SET
            {update_clause},
            updated_at = CURRENT_TIMESTAMP
        """,  # nosec B608 # columns are validated static allowlist values.
        tuple(params),
    )
    return _get_artifacts_in_transaction(conn, task_id)


class TaskArtifactManager:
    """CRUD wrapper for sparse task artifact pointers."""

    def __init__(self, db: HubDatabase):
        self.db = db

    def get_artifacts(self, task_id: str) -> TaskArtifacts:
        row = self.db.fetchone("SELECT * FROM task_artifacts WHERE task_id = ?", (task_id,))
        if row is None:
            return TaskArtifacts(task_id=task_id)
        return TaskArtifacts.from_row(row)

    def set_artifact(self, task_id: str, field: str, value: str | int | None) -> TaskArtifacts:
        return self.set_artifacts_atomic(task_id, **{field: value})

    def set_artifacts_atomic(self, task_id: str, **fields: str | int | None) -> TaskArtifacts:
        with self.db.transaction() as conn:
            return _set_artifacts_in_transaction(conn, task_id, fields)

    def clear_artifact(self, task_id: str, field: str) -> TaskArtifacts:
        _validate_field_names({field})
        return self.set_artifacts_atomic(task_id, **{field: None})

    def clear_artifacts(self, task_id: str) -> bool:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM task_artifacts WHERE task_id = ?", (task_id,))
            return cursor.rowcount > 0

    def clear_isolation_pair(self, task_id: str, family: str) -> TaskArtifacts:
        if family == "worktree":
            return self.set_artifacts_atomic(
                task_id,
                worktree_path=None,
                worktree_id=None,
                base_commit_sha=None,
            )
        if family == "clone":
            return self.set_artifacts_atomic(
                task_id,
                clone_path=None,
                clone_id=None,
                base_commit_sha=None,
            )
        raise ValueError("family must be 'worktree' or 'clone'")

    def clear_worktree_references(self, worktree_id: str) -> int:
        """Clear task artifact pointers that reference a deleted worktree id."""
        with self.db.transaction() as conn:
            rows = conn.execute(
                """
                SELECT task_id, worktree_id, integration_workspace_id
                  FROM task_artifacts
                 WHERE worktree_id = ? OR integration_workspace_id = ?
                 ORDER BY task_id
                """,
                (worktree_id, worktree_id),
            ).fetchall()
            cleared = 0
            for row in rows:
                fields: dict[str, str | int | None] = {}
                if row["worktree_id"] == worktree_id:
                    fields.update(
                        {
                            "worktree_path": None,
                            "worktree_id": None,
                            "base_commit_sha": None,
                        }
                    )
                if row["integration_workspace_id"] == worktree_id:
                    fields.update(
                        {
                            "integration_branch": None,
                            "integration_workspace_id": None,
                        }
                    )
                if fields:
                    _set_artifacts_in_transaction(conn, row["task_id"], fields)
                    cleared += 1
            return cleared

    def increment_expansion_attempts(self, task_id: str) -> int:
        current = self.get_artifacts(task_id)
        next_attempts = current.expansion_attempts + 1
        self.set_artifacts_atomic(task_id, expansion_attempts=next_attempts)
        return next_attempts


def get_artifacts(db: HubDatabase, task_id: str) -> TaskArtifacts:
    return TaskArtifactManager(db).get_artifacts(task_id)


def set_artifact(
    db: HubDatabase,
    task_id: str,
    field: str,
    value: str | int | None,
) -> TaskArtifacts:
    return TaskArtifactManager(db).set_artifact(task_id, field, value)


def set_artifacts_atomic(
    db: HubDatabase,
    task_id: str,
    **fields: str | int | None,
) -> TaskArtifacts:
    return TaskArtifactManager(db).set_artifacts_atomic(task_id, **fields)


def clear_artifact(db: HubDatabase, task_id: str, field: str) -> TaskArtifacts:
    return TaskArtifactManager(db).clear_artifact(task_id, field)


def clear_artifacts(db: HubDatabase, task_id: str) -> bool:
    return TaskArtifactManager(db).clear_artifacts(task_id)


def clear_isolation_pair(db: HubDatabase, task_id: str, family: str) -> TaskArtifacts:
    return TaskArtifactManager(db).clear_isolation_pair(task_id, family)


def increment_expansion_attempts(db: HubDatabase, task_id: str) -> int:
    return TaskArtifactManager(db).increment_expansion_attempts(task_id)
