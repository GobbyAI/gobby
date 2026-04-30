"""Add review lifecycle artifact fields."""

from __future__ import annotations

from gobby.storage.database import LocalDatabase

_TEXT_COLUMNS = ("last_reviewed_plan_hash",)
_INTEGER_COLUMNS = (
    "plan_review_attempts",
    "test_arch_attempts",
    "qa_attempts",
    "holistic_attempts",
    "merge_attempts",
)


def _columns(db: LocalDatabase) -> set[str]:
    return {row["name"] for row in db.fetchall("PRAGMA table_info(task_artifacts)")}


def up(db: LocalDatabase) -> None:
    """Add idempotent artifact fields used by lifecycle review transitions."""
    existing = _columns(db)
    for column in _TEXT_COLUMNS:
        if column not in existing:
            db.execute(f"ALTER TABLE task_artifacts ADD COLUMN {column} TEXT")  # nosec B608
    for column in _INTEGER_COLUMNS:
        if column not in existing:
            db.execute(
                f"ALTER TABLE task_artifacts ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
            )  # nosec B608


def down(db: LocalDatabase) -> None:
    """SQLite cannot drop these columns without rebuilding the table."""
    return None
