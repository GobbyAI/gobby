"""Clear failed provider output from persisted session summaries."""

from __future__ import annotations

from gobby.sessions.summary_validity import is_summary_failure_sentinel
from gobby.storage.database import LocalDatabase


def _columns(db: LocalDatabase) -> set[str]:
    return {row["name"] for row in db.fetchall("PRAGMA table_info(sessions)")}


def up(db: LocalDatabase) -> None:
    """Null out failure sentinels so summaries can regenerate from digest."""
    if "summary_markdown" not in _columns(db):
        return

    rows = db.fetchall(
        """
        SELECT id, summary_markdown
        FROM sessions
        WHERE summary_markdown IS NOT NULL
        """
    )
    sentinel_ids = [
        row["id"] for row in rows if is_summary_failure_sentinel(row["summary_markdown"])
    ]
    if not sentinel_ids:
        return

    placeholders = ", ".join("?" for _ in sentinel_ids)
    db.execute(
        f"""
        UPDATE sessions
        SET summary_markdown = NULL,
            updated_at = datetime('now')
        WHERE id IN ({placeholders})
        """,
        tuple(sentinel_ids),
    )


def down(db: LocalDatabase) -> None:
    """Cleared failure sentinels are intentionally not restored."""
    return None
