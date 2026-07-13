from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager

logger = logging.getLogger(__name__)

__all__ = [
    "NullProjectMemoryRepair",
    "NullProjectMemoryRepairResult",
    "NullProjectMemoryRepairService",
    "find_null_project_repairs",
]


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
    failed_secondary_updates: list[dict[str, str]] = field(default_factory=list)


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


class NullProjectMemoryRepairService:
    """Repair memory project IDs inferred from source sessions."""

    def __init__(
        self,
        *,
        db: HubDatabase,
        storage_provider: Callable[[], LocalMemoryManager],
        run_db: Callable[..., Awaitable[Any]],
        embed_and_upsert: Callable[..., Awaitable[bool]],
    ) -> None:
        self._db = db
        self._storage_provider = storage_provider
        self._run_db = run_db
        self._embed_and_upsert = embed_and_upsert

    @property
    def storage(self) -> LocalMemoryManager:
        return self._storage_provider()

    async def fix_null_project_ids_from_sessions(
        self,
        *,
        dry_run: bool = False,
    ) -> NullProjectMemoryRepairResult:
        """Repair NULL memory project IDs using each memory's source session."""
        repairs = await self._run_db(find_null_project_repairs, self._db)
        fixable_repairs = [repair for repair in repairs if repair.project_id is not None]

        fixed = 0
        failed_secondary_updates: list[dict[str, str]] = []
        if not dry_run:
            for repair in fixable_repairs:
                project_id = cast(str, repair.project_id)
                updated = await self._run_db(
                    self.storage.update_memory_project,
                    repair.memory_id,
                    project_id,
                )
                fixed += 1
                try:
                    await self._embed_and_upsert(
                        repair.memory_id,
                        updated.content,
                        payload={"project_id": project_id},
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to update embedding for repaired memory %s: %s",
                        repair.memory_id,
                        exc,
                    )
                    failed_secondary_updates.append(
                        {
                            "memory_id": repair.memory_id,
                            "index": "embedding",
                            "error": str(exc),
                        }
                    )
                try:
                    self.storage.mark_pending_graph(repair.memory_id)
                except Exception as exc:
                    logger.warning(
                        "Failed to queue repaired memory %s for graph indexing: %s",
                        repair.memory_id,
                        exc,
                    )
                    failed_secondary_updates.append(
                        {
                            "memory_id": repair.memory_id,
                            "index": "knowledge_graph",
                            "error": str(exc),
                        }
                    )

        return NullProjectMemoryRepairResult(
            total=len(repairs),
            fixable=len(fixable_repairs),
            fixed=fixed,
            repairs=repairs,
            failed_secondary_updates=failed_secondary_updates,
        )
