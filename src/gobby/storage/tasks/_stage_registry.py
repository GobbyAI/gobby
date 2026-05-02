"""Task stage registry storage manager."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from gobby.storage.database import DatabaseProtocol

ReviewPolicy = Literal["none", "required", "optional"]
StageCategory = Literal["discovery", "design", "verification", "implementation", "delivery"]

_REQUIRED_POLICY_STAGES: dict[str, str | None] = {
    "planning": "plan-adversary",
    "expansion": "expansion-qa",
    "development": "qa-reviewer",
    "holistic_qa": "holistic-reviewer",
    "pr": None,
}


@dataclass(frozen=True, slots=True)
class StageRegistryEntry:
    name: str
    display_label: str
    description: str
    category: StageCategory
    default_agent: str | None
    reviewer_agent: str | None
    review_policy: ReviewPolicy
    position_hint: int
    requires_human: bool
    is_terminal: bool
    default_max_work_attempts: int
    default_max_review_rounds: int


class StageRegistryManager:
    def __init__(self, db: DatabaseProtocol) -> None:
        self.db = db
        self._ensure_phase2_columns()

    def list_all(self) -> list[StageRegistryEntry]:
        rows = self.db.fetchall(
            """
            SELECT *
              FROM task_stages_registry
             ORDER BY position_hint, name
            """
        )
        return [self._entry_from_row(row) for row in rows]

    def get(self, name: str) -> StageRegistryEntry | None:
        row = self.db.fetchone(
            "SELECT * FROM task_stages_registry WHERE name = ?",
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
                    reviewer_agent, review_policy, position_hint, requires_human,
                    is_terminal, default_max_work_attempts, default_max_review_rounds,
                    bundled_hash, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(name) DO UPDATE SET
                    display_label = excluded.display_label,
                    description = excluded.description,
                    category = excluded.category,
                    default_agent = excluded.default_agent,
                    reviewer_agent = excluded.reviewer_agent,
                    review_policy = excluded.review_policy,
                    position_hint = excluded.position_hint,
                    requires_human = excluded.requires_human,
                    is_terminal = excluded.is_terminal,
                    default_max_work_attempts = excluded.default_max_work_attempts,
                    default_max_review_rounds = excluded.default_max_review_rounds,
                    bundled_hash = COALESCE(excluded.bundled_hash, task_stages_registry.bundled_hash),
                    updated_at = datetime('now')
                """,
                (
                    entry.name,
                    entry.display_label,
                    entry.description,
                    entry.category,
                    entry.default_agent,
                    entry.reviewer_agent,
                    entry.review_policy,
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

    def _entry_from_row(self, row) -> StageRegistryEntry:
        review_policy = self._row_value(row, "review_policy")
        reviewer_agent = self._row_value(row, "reviewer_agent")
        if row["name"] in _REQUIRED_POLICY_STAGES:
            review_policy = "required"
            reviewer_agent = _REQUIRED_POLICY_STAGES[row["name"]]
        elif review_policy not in {"none", "required", "optional"}:
            review_policy = "none"

        return StageRegistryEntry(
            name=row["name"],
            display_label=row["display_label"],
            description=row["description"],
            category=row["category"],
            default_agent=self._row_value(row, "default_agent"),
            reviewer_agent=reviewer_agent,
            review_policy=review_policy,
            position_hint=int(row["position_hint"]),
            requires_human=bool(row["requires_human"]),
            is_terminal=bool(row["is_terminal"]),
            default_max_work_attempts=int(self._row_value(row, "default_max_work_attempts") or 3),
            default_max_review_rounds=int(self._row_value(row, "default_max_review_rounds") or 5),
        )

    def _ensure_phase2_columns(self) -> None:
        columns = self._columns("task_stages_registry")
        additions = {
            "reviewer_agent": "ALTER TABLE task_stages_registry ADD COLUMN reviewer_agent TEXT",
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
        }
        with self.db.transaction() as conn:
            for column, sql in additions.items():
                if column not in columns:
                    conn.execute(sql)
            for stage_name, reviewer_agent in _REQUIRED_POLICY_STAGES.items():
                conn.execute(
                    """
                    UPDATE task_stages_registry
                       SET review_policy = 'required',
                           reviewer_agent = ?,
                           updated_at = datetime('now')
                     WHERE name = ?
                    """,
                    (reviewer_agent, stage_name),
                )

    def _columns(self, table_name: str) -> set[str]:
        return {row["name"] for row in self.db.fetchall(f"PRAGMA table_info({table_name})")}

    @staticmethod
    def _row_value(row, column: str):
        try:
            return row[column]
        except (IndexError, KeyError):
            return None

    @staticmethod
    def _validate_entry(entry: StageRegistryEntry) -> None:
        if entry.review_policy not in {"none", "required", "optional"}:
            raise ValueError(f"Invalid review_policy '{entry.review_policy}'")
        if entry.review_policy != "none" and not entry.reviewer_agent and entry.name != "pr":
            raise ValueError(
                f"reviewer_agent is required for stage '{entry.name}' with review policy"
            )
        if entry.default_max_work_attempts < 1:
            raise ValueError("default_max_work_attempts must be >= 1")
        if entry.default_max_review_rounds < 1:
            raise ValueError("default_max_review_rounds must be >= 1")
