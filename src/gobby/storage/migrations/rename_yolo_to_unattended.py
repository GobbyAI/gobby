"""Rename tasks.yolo to tasks.unattended."""

from __future__ import annotations

from gobby.storage.database import LocalDatabase


def _task_columns(db: LocalDatabase) -> set[str]:
    return {row["name"] for row in db.fetchall("PRAGMA table_info(tasks)")}


def up(db: LocalDatabase) -> None:
    """Rename the legacy yolo column to unattended."""
    columns = _task_columns(db)
    if "unattended" in columns:
        return
    if "yolo" not in columns:
        db.execute(
            """
            ALTER TABLE tasks
            ADD COLUMN unattended INTEGER NOT NULL DEFAULT 0 CHECK(unattended IN (0, 1))
            """
        )
        return
    db.execute("ALTER TABLE tasks RENAME COLUMN yolo TO unattended")


def down(db: LocalDatabase) -> None:
    """Restore the legacy yolo column name."""
    columns = _task_columns(db)
    if "yolo" in columns:
        return
    if "unattended" not in columns:
        db.execute(
            """
            ALTER TABLE tasks
            ADD COLUMN yolo INTEGER NOT NULL DEFAULT 0 CHECK(yolo IN (0, 1))
            """
        )
        return
    db.execute("ALTER TABLE tasks RENAME COLUMN unattended TO yolo")
