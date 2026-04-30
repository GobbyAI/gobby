"""Add system-managed marker to cron jobs."""

from __future__ import annotations

from gobby.storage.database import LocalDatabase


def _columns(db: LocalDatabase) -> set[str]:
    return {row["name"] for row in db.fetchall("PRAGMA table_info(cron_jobs)")}


def up(db: LocalDatabase) -> None:
    """Add an idempotent marker for bundled/system cron rows."""
    if "is_system" not in _columns(db):
        db.execute(
            """
            ALTER TABLE cron_jobs
            ADD COLUMN is_system INTEGER NOT NULL DEFAULT 0 CHECK(is_system IN (0, 1))
            """
        )


def down(db: LocalDatabase) -> None:
    """Drop the system marker."""
    if "is_system" in _columns(db):
        db.execute("ALTER TABLE cron_jobs DROP COLUMN is_system")
