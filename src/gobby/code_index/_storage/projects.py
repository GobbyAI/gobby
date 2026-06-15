"""Project stats and project-wide delete helpers."""

from __future__ import annotations

from typing import cast

from gobby.code_index.models import IndexedProject
from gobby.storage.hub.protocol import HubDatabase


class CodeIndexProjectStorageMixin:
    """Storage methods for project-level index state."""

    db: HubDatabase

    def upsert_project_stats(self, project: IndexedProject) -> None:
        """Insert or update project statistics."""
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO code_indexed_projects (
                    id, root_path, total_files, total_symbols,
                    last_indexed_at, index_duration_ms
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT(id) DO UPDATE SET
                    total_files=excluded.total_files,
                    total_symbols=excluded.total_symbols,
                    last_indexed_at=excluded.last_indexed_at,
                    index_duration_ms=excluded.index_duration_ms,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    project.id,
                    project.root_path,
                    project.total_files,
                    project.total_symbols,
                    project.last_indexed_at or None,
                    project.index_duration_ms,
                ),
            )

    def get_project_stats(self, project_id: str) -> IndexedProject | None:
        """Get project statistics."""
        row = self.db.fetchone(
            "SELECT * FROM code_indexed_projects WHERE id = %s",
            (project_id,),
        )
        return IndexedProject.from_row(row) if row else None

    def list_indexed_projects(self) -> list[IndexedProject]:
        """List all indexed projects."""
        rows = self.db.fetchall("SELECT * FROM code_indexed_projects ORDER BY last_indexed_at DESC")
        return [IndexedProject.from_row(r) for r in rows]

    def delete_project_index(self, project_id: str) -> dict[str, int]:
        """Delete all persisted index data for a project, including project stats."""
        with self.db.transaction() as conn:
            counts = {
                "symbols": conn.execute(
                    "DELETE FROM code_symbols WHERE project_id = %s",
                    (project_id,),
                ).rowcount,
                "files": conn.execute(
                    "DELETE FROM code_indexed_files WHERE project_id = %s",
                    (project_id,),
                ).rowcount,
                "imports": conn.execute(
                    "DELETE FROM code_imports WHERE project_id = %s",
                    (project_id,),
                ).rowcount,
                "calls": conn.execute(
                    "DELETE FROM code_calls WHERE project_id = %s",
                    (project_id,),
                ).rowcount,
                "content_chunks": conn.execute(
                    "DELETE FROM code_content_chunks WHERE project_id = %s",
                    (project_id,),
                ).rowcount,
            }
            cursor = conn.execute("DELETE FROM code_indexed_projects WHERE id = %s", (project_id,))
            counts["projects"] = cursor.rowcount
            return counts

    def count_symbols(self, project_id: str) -> int:
        """Count total symbols for a project."""
        row = self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM code_symbols WHERE project_id = %s",
            (project_id,),
        )
        return cast(int, row["cnt"]) if row else 0

    def count_files(self, project_id: str) -> int:
        """Count total indexed files for a project."""
        row = self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM code_indexed_files WHERE project_id = %s",
            (project_id,),
        )
        return cast(int, row["cnt"]) if row else 0
