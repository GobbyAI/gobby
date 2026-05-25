"""Read-side helpers for persisted task stage-state rows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.storage.tasks._stage_registry import StageRegistryEntry, StageRegistryManager
from gobby.storage.tasks._stage_types import StageManifestSpec, StageState, _coerce_artifact_refs

VALID_STAGE_STATES = frozenset({"ready", "in_progress", "needs_review", "review_approved", "done"})
_StageStateReader = HubDatabase | Transaction


def row_value(row: Mapping[str, Any], column: str) -> Any:
    try:
        return row[column]
    except (IndexError, KeyError):
        return None


def state_from_row(
    row: Mapping[str, Any], registry: StageRegistryManager | None = None
) -> StageState:
    display_label = row_value(row, "display_label")
    category = row_value(row, "category")
    if (display_label is None or category is None) and registry is not None:
        registry_entry = registry.get(row["stage_name"])
        if registry_entry is not None:
            display_label = display_label or registry_entry.display_label
            category = category or registry_entry.category
    return StageState(
        task_id=row["task_id"],
        stage_name=row["stage_name"],
        position=int(row["position"]),
        state=row["state"],
        review_policy=row["review_policy"],
        reviewer_agent=row["reviewer_agent"],
        entered_at=row["entered_at"],
        entered_by_session_id=row["entered_by_session_id"],
        completed_at=row["completed_at"],
        completed_by_session_id=row["completed_by_session_id"],
        completed_commit_sha=row["completed_commit_sha"],
        work_attempt_count=int(row["work_attempt_count"]),
        review_round_count=int(row["review_round_count"]),
        max_work_attempts=row["max_work_attempts"],
        max_review_rounds=row["max_review_rounds"],
        artifact_refs=_coerce_artifact_refs(row["artifact_refs"]),
        notes=row["notes"],
        updated_at=row["updated_at"],
        display_name=display_label,
        display_label=display_label,
        category=category,
    )


def shape_signature_for_specs(specs: Sequence[StageManifestSpec]) -> str:
    return ",".join(f"{item.position}:{item.stage_name}:ready" for item in specs)


def validate_state_value(value: str) -> None:
    if value not in VALID_STAGE_STATES:
        raise ValueError(f"Invalid stage state '{value}'")


class StageStateRows:
    def __init__(self, db: HubDatabase, registry: StageRegistryManager) -> None:
        self.db = db
        self.registry = registry

    def list_for_task(
        self,
        task_id: str,
        *,
        reader: _StageStateReader | None = None,
    ) -> list[StageState]:
        active_reader = reader or self.db
        rows = active_reader.execute(
            """
            SELECT *
              FROM task_stage_states
             WHERE task_id = ?
             ORDER BY position, stage_name
            """,
            (task_id,),
        ).fetchall()
        return [self.state_from_row(row) for row in rows]

    def get(
        self,
        task_id: str,
        stage_name: str,
        *,
        reader: _StageStateReader | None = None,
    ) -> StageState | None:
        active_reader = reader or self.db
        row = active_reader.execute(
            """
            SELECT *
              FROM task_stage_states
             WHERE task_id = ? AND stage_name = ?
            """,
            (task_id, stage_name),
        ).fetchone()
        return self.state_from_row(row) if row is not None else None

    def current_stage(
        self,
        task_id: str,
        *,
        reader: _StageStateReader | None = None,
    ) -> StageState | None:
        active_reader = reader or self.db
        row = active_reader.execute(
            """
            SELECT *
              FROM task_stage_states
             WHERE task_id = ? AND state != 'done'
             ORDER BY position, stage_name
             LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        return self.state_from_row(row) if row is not None else None

    def list_tasks_at_stage(
        self,
        *,
        stage_name: str,
        state: str | None = None,
        project_id: str | None = None,
    ) -> list[str]:
        params: list[object] = [stage_name]
        filters = ["s.stage_name = ?"]
        if state is not None:
            validate_state_value(state)
            filters.append("s.state = ?")
            params.append(state)
        if project_id is not None:
            filters.append("t.project_id = ?")
            params.append(project_id)
        rows = self.db.fetchall(
            f"""
            SELECT s.task_id
              FROM task_stage_states s
              JOIN tasks t ON t.id = s.task_id
             WHERE {" AND ".join(filters)}
             ORDER BY t.created_at, s.task_id
            """,  # nosec B608 # filters are assembled from fixed clauses.
            tuple(params),
        )
        return [row["task_id"] for row in rows]

    def state_from_row(self, row: Mapping[str, Any]) -> StageState:
        return state_from_row(row, self.registry)

    def validate_specs(self, specs: Sequence[StageManifestSpec]) -> None:
        if not specs:
            raise ValueError("manifest must contain at least one stage")
        seen_names: set[str] = set()
        seen_positions: set[int] = set()
        for item in specs:
            if self.registry.get(item.stage_name) is None:
                raise ValueError(f"Unknown stage '{item.stage_name}'")
            if item.stage_name in seen_names:
                raise ValueError(f"Duplicate stage '{item.stage_name}'")
            if item.position in seen_positions:
                raise ValueError(f"Duplicate stage position {item.position}")
            seen_names.add(item.stage_name)
            seen_positions.add(item.position)

    def registry_entry(self, stage_name: str) -> StageRegistryEntry:
        entry = self.registry.get(stage_name)
        if entry is None:
            raise ValueError(f"Unknown stage '{stage_name}'")
        return entry

    def shape_signature(self, task_id: str) -> str:
        return ",".join(
            f"{row.position}:{row.stage_name}:{row.state}" for row in self.list_for_task(task_id)
        )
