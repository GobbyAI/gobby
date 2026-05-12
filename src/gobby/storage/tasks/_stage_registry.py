"""Task stage registry storage manager."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._stage_reviewer_selector import (
    ReviewerAgentSelectorError,
    validate_reviewer_agent_selector_json,
)

ReviewPolicy = Literal["none", "required", "optional"]
StageCategory = Literal["discovery", "design", "verification", "implementation", "delivery"]
DispatchType = Literal["agent", "pipeline"]


@dataclass(frozen=True, slots=True)
class StageRegistryEntry:
    name: str
    display_label: str
    description: str
    category: StageCategory
    default_agent: str | None
    reviewer_agent: str | None
    reviewer_agent_selector_json: str | None
    review_policy: ReviewPolicy
    dispatch_type: DispatchType | None
    dispatch_target: str | None
    dispatch_inputs_json: str | None
    position_hint: int
    requires_human: bool
    is_terminal: bool
    default_max_work_attempts: int
    default_max_review_rounds: int
    bundled_hash: str | None = None
    deleted_at: str | None = None
    is_edited: bool = False


class StageRegistryManager:
    def __init__(self, db: DatabaseProtocol) -> None:
        self.db = db
        self._ensure_phase2_columns()

    def list_all(self, *, include_deleted: bool = False) -> list[StageRegistryEntry]:
        deleted_filter = "" if include_deleted else "WHERE deleted_at IS NULL"
        rows = self.db.fetchall(
            f"""
            SELECT *
              FROM task_stages_registry
             {deleted_filter}
             ORDER BY position_hint, name
            """  # nosec B608 - deleted_filter is controlled by a boolean.
        )
        return [self._entry_from_row(row) for row in rows]

    def get(self, name: str, *, include_deleted: bool = False) -> StageRegistryEntry | None:
        deleted_filter = "" if include_deleted else "AND deleted_at IS NULL"
        row = self.db.fetchone(
            f"""
            SELECT *
              FROM task_stages_registry
             WHERE name = ?
               {deleted_filter}
            """,  # nosec B608 - deleted_filter is controlled by a boolean.
            (name,),
        )
        return self._entry_from_row(row) if row is not None else None

    def upsert(self, entry: StageRegistryEntry, *, bundled_hash: str | None = None) -> None:
        self._validate_entry(entry)
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO task_stages_registry (
                    name, display_label, description, category, default_agent,
                    reviewer_agent, reviewer_agent_selector_json, review_policy,
                    dispatch_type, dispatch_target, dispatch_inputs_json, position_hint,
                    requires_human, is_terminal, default_max_work_attempts,
                    default_max_review_rounds, bundled_hash, deleted_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, datetime('now'))
                ON CONFLICT(name) DO UPDATE SET
                    display_label = excluded.display_label,
                    description = excluded.description,
                    category = excluded.category,
                    default_agent = excluded.default_agent,
                    reviewer_agent = excluded.reviewer_agent,
                    reviewer_agent_selector_json = excluded.reviewer_agent_selector_json,
                    review_policy = excluded.review_policy,
                    dispatch_type = excluded.dispatch_type,
                    dispatch_target = excluded.dispatch_target,
                    dispatch_inputs_json = excluded.dispatch_inputs_json,
                    position_hint = excluded.position_hint,
                    requires_human = excluded.requires_human,
                    is_terminal = excluded.is_terminal,
                    default_max_work_attempts = excluded.default_max_work_attempts,
                    default_max_review_rounds = excluded.default_max_review_rounds,
                    bundled_hash = COALESCE(excluded.bundled_hash, task_stages_registry.bundled_hash),
                    deleted_at = NULL,
                    updated_at = datetime('now')
                """,
                (
                    entry.name,
                    entry.display_label,
                    entry.description,
                    entry.category,
                    entry.default_agent,
                    entry.reviewer_agent,
                    entry.reviewer_agent_selector_json,
                    entry.review_policy,
                    entry.dispatch_type,
                    entry.dispatch_target,
                    entry.dispatch_inputs_json,
                    entry.position_hint,
                    1 if entry.requires_human else 0,
                    1 if entry.is_terminal else 0,
                    entry.default_max_work_attempts,
                    entry.default_max_review_rounds,
                    bundled_hash,
                ),
            )

    def list_default_stages(self, task_type: str) -> list[tuple[str, int]]:
        rows = self.db.fetchall(
            """
            SELECT stage_name, position
              FROM task_type_default_stages
             WHERE task_type = ?
             ORDER BY position, stage_name
            """,
            (task_type,),
        )
        return [(row["stage_name"], int(row["position"])) for row in rows]

    def set_default_stages(self, task_type: str, stages: Sequence[tuple[str, int]]) -> None:
        if not task_type:
            raise ValueError("task_type is required")
        seen_names: set[str] = set()
        seen_positions: set[int] = set()
        for stage_name, position in stages:
            if self.get(stage_name) is None:
                raise ValueError(f"Unknown stage '{stage_name}'")
            if stage_name in seen_names:
                raise ValueError(f"Duplicate default stage '{stage_name}'")
            if position in seen_positions:
                raise ValueError(f"Duplicate default stage position {position}")
            seen_names.add(stage_name)
            seen_positions.add(position)

        with self.db.transaction() as conn:
            conn.execute("DELETE FROM task_type_default_stages WHERE task_type = ?", (task_type,))
            conn.executemany(
                """
                INSERT INTO task_type_default_stages (task_type, stage_name, position)
                VALUES (?, ?, ?)
                """,
                [(task_type, stage_name, position) for stage_name, position in stages],
            )

    def update_stage(self, name: str, updates: dict[str, Any]) -> StageRegistryEntry:
        """Update editable stage metadata. Stage names are immutable."""

        if "name" in updates and updates["name"] != name:
            raise ValueError("stage names are immutable")
        current = self.get(name)
        if current is None:
            raise ValueError(f"Unknown stage '{name}'")
        payload = self._entry_payload(current)
        payload.update({key: value for key, value in updates.items() if key != "name"})
        entry = StageRegistryEntry(**payload)
        self._validate_entry(entry)
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE task_stages_registry
                   SET display_label = ?,
                       description = ?,
                       category = ?,
                       default_agent = ?,
                       reviewer_agent = ?,
                       reviewer_agent_selector_json = ?,
                       review_policy = ?,
                       dispatch_type = ?,
                       dispatch_target = ?,
                       dispatch_inputs_json = ?,
                       position_hint = ?,
                       requires_human = ?,
                       is_terminal = ?,
                       default_max_work_attempts = ?,
                       default_max_review_rounds = ?,
                       updated_at = datetime('now')
                 WHERE name = ?
                   AND deleted_at IS NULL
                """,
                (
                    entry.display_label,
                    entry.description,
                    entry.category,
                    entry.default_agent,
                    entry.reviewer_agent,
                    entry.reviewer_agent_selector_json,
                    entry.review_policy,
                    entry.dispatch_type,
                    entry.dispatch_target,
                    entry.dispatch_inputs_json,
                    entry.position_hint,
                    1 if entry.requires_human else 0,
                    1 if entry.is_terminal else 0,
                    entry.default_max_work_attempts,
                    entry.default_max_review_rounds,
                    name,
                ),
            )
        updated = self.get(name)
        if updated is None:
            raise ValueError(f"Unknown stage '{name}'")
        return updated

    def restore_stage(self, name: str) -> StageRegistryEntry:
        from gobby.storage.tasks._stage_registry_loader import StageRegistryLoader

        bundled = {entry.name: entry for entry in StageRegistryLoader().load_with_hashes()}
        bundled_entry = bundled.get(name)
        if bundled_entry is None:
            raise ValueError(f"Stage '{name}' is not bundled and cannot be restored")
        self.upsert(bundled_entry.to_registry_entry(), bundled_hash=bundled_entry.bundled_hash)
        restored = self.get(name)
        if restored is None:
            raise ValueError(f"Stage '{name}' could not be restored")
        return restored

    def delete_stage(self, name: str) -> StageRegistryEntry:
        current = self.get(name)
        if current is None:
            raise ValueError(f"Unknown stage '{name}'")
        blocker = self._delete_blocker(name)
        if blocker is not None:
            raise ValueError(blocker)
        deleted_at = datetime.now(UTC).isoformat()
        self.db.execute(
            """
            UPDATE task_stages_registry
               SET deleted_at = ?, updated_at = datetime('now')
             WHERE name = ?
            """,
            (deleted_at, name),
        )
        deleted = self.get(name, include_deleted=True)
        if deleted is None:
            raise ValueError(f"Stage '{name}' could not be deleted")
        return deleted

    def _entry_from_row(self, row: sqlite3.Row) -> StageRegistryEntry:
        review_policy = self._row_value(row, "review_policy")
        if review_policy not in {"none", "required", "optional"}:
            review_policy = "none"

        return StageRegistryEntry(
            name=row["name"],
            display_label=row["display_label"],
            description=row["description"],
            category=row["category"],
            default_agent=self._row_value(row, "default_agent"),
            reviewer_agent=self._row_value(row, "reviewer_agent"),
            reviewer_agent_selector_json=self._row_value(row, "reviewer_agent_selector_json"),
            review_policy=review_policy,
            dispatch_type=self._dispatch_type_from_row(row),
            dispatch_target=self._row_value(row, "dispatch_target"),
            dispatch_inputs_json=self._row_value(row, "dispatch_inputs_json"),
            position_hint=int(row["position_hint"]),
            requires_human=bool(row["requires_human"]),
            is_terminal=bool(row["is_terminal"]),
            default_max_work_attempts=int(self._row_value(row, "default_max_work_attempts") or 3),
            default_max_review_rounds=int(self._row_value(row, "default_max_review_rounds") or 5),
            bundled_hash=self._row_value(row, "bundled_hash"),
            deleted_at=self._row_value(row, "deleted_at"),
            is_edited=self._is_row_edited(row),
        )

    def _ensure_phase2_columns(self) -> None:
        columns = self._columns("task_stages_registry")
        additions = {
            "reviewer_agent": "ALTER TABLE task_stages_registry ADD COLUMN reviewer_agent TEXT",
            "reviewer_agent_selector_json": (
                "ALTER TABLE task_stages_registry ADD COLUMN reviewer_agent_selector_json TEXT"
            ),
            "review_policy": (
                "ALTER TABLE task_stages_registry ADD COLUMN review_policy TEXT "
                "NOT NULL DEFAULT 'none'"
            ),
            "default_max_work_attempts": (
                "ALTER TABLE task_stages_registry ADD COLUMN default_max_work_attempts "
                "INTEGER NOT NULL DEFAULT 3"
            ),
            "default_max_review_rounds": (
                "ALTER TABLE task_stages_registry ADD COLUMN default_max_review_rounds "
                "INTEGER NOT NULL DEFAULT 5"
            ),
            "dispatch_type": "ALTER TABLE task_stages_registry ADD COLUMN dispatch_type TEXT",
            "dispatch_target": "ALTER TABLE task_stages_registry ADD COLUMN dispatch_target TEXT",
            "dispatch_inputs_json": (
                "ALTER TABLE task_stages_registry ADD COLUMN dispatch_inputs_json TEXT"
            ),
            "deleted_at": "ALTER TABLE task_stages_registry ADD COLUMN deleted_at TEXT",
        }
        with self.db.transaction() as conn:
            for column, sql in additions.items():
                if column not in columns:
                    conn.execute(sql)

    def _columns(self, table_name: str) -> set[str]:
        return {row["name"] for row in self.db.fetchall(f"PRAGMA table_info({table_name})")}

    @staticmethod
    def _row_value(row: sqlite3.Row, column: str) -> Any:
        try:
            return row[column]
        except (IndexError, KeyError):
            return None

    @classmethod
    def _dispatch_type_from_row(cls, row: sqlite3.Row) -> DispatchType | None:
        value = cls._row_value(row, "dispatch_type")
        return value if value in {"agent", "pipeline"} else None

    @classmethod
    def _is_row_edited(cls, row: sqlite3.Row) -> bool:
        bundled_hash = cls._row_value(row, "bundled_hash")
        if not bundled_hash:
            return False
        return cls.row_hash(row) != str(bundled_hash)

    @classmethod
    def row_hash(cls, row: sqlite3.Row | dict[str, Any]) -> str:
        import hashlib

        body = json.dumps(
            cls._row_canonical_payload(row),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(body).hexdigest()

    @classmethod
    def _row_canonical_payload(cls, row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        def value(key: str) -> Any:
            if isinstance(row, dict):
                return row.get(key)
            return cls._row_value(row, key)

        return {
            "name": value("name"),
            "display_label": value("display_label"),
            "description": value("description"),
            "category": value("category"),
            "default_agent": value("default_agent"),
            "reviewer_agent": value("reviewer_agent"),
            "reviewer_agent_selector_json": value("reviewer_agent_selector_json"),
            "review_policy": value("review_policy") or "none",
            "dispatch_type": value("dispatch_type"),
            "dispatch_target": value("dispatch_target"),
            "dispatch_inputs_json": value("dispatch_inputs_json"),
            "position_hint": int(value("position_hint") or 0),
            "requires_human": bool(value("requires_human")),
            "is_terminal": bool(value("is_terminal")),
            "default_max_work_attempts": int(value("default_max_work_attempts") or 3),
            "default_max_review_rounds": int(value("default_max_review_rounds") or 5),
        }

    @staticmethod
    def _entry_payload(entry: StageRegistryEntry) -> dict[str, Any]:
        return {
            "name": entry.name,
            "display_label": entry.display_label,
            "description": entry.description,
            "category": entry.category,
            "default_agent": entry.default_agent,
            "reviewer_agent": entry.reviewer_agent,
            "reviewer_agent_selector_json": entry.reviewer_agent_selector_json,
            "review_policy": entry.review_policy,
            "dispatch_type": entry.dispatch_type,
            "dispatch_target": entry.dispatch_target,
            "dispatch_inputs_json": entry.dispatch_inputs_json,
            "position_hint": entry.position_hint,
            "requires_human": entry.requires_human,
            "is_terminal": entry.is_terminal,
            "default_max_work_attempts": entry.default_max_work_attempts,
            "default_max_review_rounds": entry.default_max_review_rounds,
        }

    def _delete_blocker(self, name: str) -> str | None:
        active_state = self.db.fetchone(
            """
            SELECT task_id
              FROM task_stage_states
             WHERE stage_name = ?
               AND state != 'done'
             LIMIT 1
            """,
            (name,),
        )
        if active_state is not None:
            return f"Stage '{name}' is referenced by active task stage states"
        default_ref = self.db.fetchone(
            "SELECT task_type FROM task_type_default_stages WHERE stage_name = ? LIMIT 1",
            (name,),
        )
        if default_ref is not None:
            return f"Stage '{name}' is referenced by task type defaults"
        build_profiles = self.db.fetchone(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'build_profiles'"
        )
        if build_profiles is None:
            return None
        profile_ref = self.db.fetchone(
            """
            SELECT name
              FROM build_profiles
             WHERE deleted_at IS NULL
               AND skip_stages_json LIKE ?
             LIMIT 1
            """,
            (f'%"{name}"%',),
        )
        if profile_ref is not None:
            return f"Stage '{name}' is referenced by build profile '{profile_ref['name']}'"
        return None

    @staticmethod
    def _validate_entry(entry: StageRegistryEntry) -> None:
        if entry.review_policy not in {"none", "required", "optional"}:
            raise ValueError(f"Invalid review_policy '{entry.review_policy}'")
        if entry.dispatch_type not in {None, "agent", "pipeline"}:
            raise ValueError(f"Invalid dispatch_type '{entry.dispatch_type}'")
        if entry.dispatch_type == "pipeline" and not entry.dispatch_target:
            raise ValueError("dispatch_target is required for pipeline dispatch")
        if entry.dispatch_type == "agent" and not (entry.dispatch_target or entry.default_agent):
            raise ValueError("dispatch_target or default_agent is required for agent dispatch")
        if entry.dispatch_inputs_json:
            try:
                dispatch_inputs = json.loads(entry.dispatch_inputs_json)
            except json.JSONDecodeError as exc:
                raise ValueError("dispatch_inputs_json must be valid JSON") from exc
            if not isinstance(dispatch_inputs, dict):
                raise ValueError("dispatch_inputs_json must be a JSON object")
        try:
            validate_reviewer_agent_selector_json(
                entry.reviewer_agent_selector_json,
                stage_name=entry.name,
            )
        except ReviewerAgentSelectorError as exc:
            raise ValueError(str(exc)) from exc
        if (
            entry.review_policy != "none"
            and not entry.reviewer_agent
            and not entry.reviewer_agent_selector_json
            and entry.name != "pr"
        ):
            raise ValueError(
                f"reviewer_agent or reviewer_agent_selector_json is required for "
                f"stage '{entry.name}' with review policy"
            )
        if entry.default_max_work_attempts < 1:
            raise ValueError("default_max_work_attempts must be >= 1")
        if entry.default_max_review_rounds < 1:
            raise ValueError("default_max_review_rounds must be >= 1")
