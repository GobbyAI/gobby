from __future__ import annotations

from dataclasses import dataclass

from gobby.storage.hub.protocol import HubDatabase

__all__ = ["NullProjectMemoryRepair", "NullProjectMemoryRepairResult", "find_null_project_repairs"]


@dataclass(frozen=True)
class NullProjectMemoryRepair:
    """Repair candidate for a memory with a missing project assignment."""

    memory_id: str
    content: str | None
    source_session_id: str
    project_id: str | None


@dataclass(frozen=True)
class NullProjectMemoryRepairResult:
    """Result of repairing memories whose project can be inferred from sessions."""

    total: int
    fixable: int
    fixed: int
    repairs: list[NullProjectMemoryRepair]


def find_null_project_repairs(db: HubDatabase) -> list[NullProjectMemoryRepair]:
    """Find memories whose missing project can be inferred from source sessions."""
    rows = db.fetchall(
        """
        SELECT id, content, source_session_id
        FROM memories
        WHERE project_id IS NULL
          AND source_type IN ('session', 'agent')
          AND source_session_id IS NOT NULL
        """,
        (),
    )
    if not rows:
        return []

    session_ids = {row["source_session_id"] for row in rows if row["source_session_id"]}
    session_project_ids: dict[str, str] = {}
    if session_ids:
        placeholders = ",".join("%s" for _ in session_ids)
        session_rows = db.fetchall(
            f"SELECT id, project_id FROM sessions WHERE id IN ({placeholders})",  # nosec B608
            tuple(session_ids),
        )
        session_project_ids = {
            row["id"]: row["project_id"] for row in session_rows if row["project_id"]
        }

    return [
        NullProjectMemoryRepair(
            memory_id=row["id"],
            content=row["content"],
            source_session_id=row["source_session_id"],
            project_id=session_project_ids.get(row["source_session_id"]),
        )
        for row in rows
    ]
