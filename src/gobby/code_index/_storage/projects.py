"""Project stats and project-wide delete helpers."""

from __future__ import annotations

from typing import cast

from gobby.code_index.models import IndexedProject
from gobby.servers.lease_fence import run_hub_mutation
from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.utils.machine_id import require_machine_id


class CodeIndexProjectStorageMixin:
    """Storage methods for project-level index state."""

    db: HubDatabase

    def upsert_project_stats(self, project: IndexedProject) -> None:
        """Insert shared project identity and this machine's local state."""
        machine_id = require_machine_id()
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO code_indexed_projects (id) VALUES (%s)
                ON CONFLICT(id) DO UPDATE SET updated_at=CURRENT_TIMESTAMP
                """,
                (project.id,),
            )
            conn.execute(
                """INSERT INTO code_indexed_project_states (
                    machine_id, project_id, root_path, total_files, total_symbols,
                    last_indexed_at, index_duration_ms
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(machine_id, project_id) DO UPDATE SET
                    root_path=excluded.root_path,
                    total_files=excluded.total_files,
                    total_symbols=excluded.total_symbols,
                    last_indexed_at=excluded.last_indexed_at,
                    index_duration_ms=excluded.index_duration_ms,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    machine_id,
                    project.id,
                    project.root_path,
                    project.total_files,
                    project.total_symbols,
                    project.last_indexed_at or None,
                    project.index_duration_ms,
                ),
            )

    def get_project_stats(self, project_id: str) -> IndexedProject | None:
        """Get this machine's project statistics."""
        row = self.db.fetchone(
            """SELECT project_id AS id, root_path, total_files, total_symbols,
                      last_indexed_at, index_duration_ms
               FROM code_indexed_project_states
               WHERE machine_id = %s AND project_id = %s""",
            (require_machine_id(), project_id),
        )
        return IndexedProject.from_row(row) if row else None

    def list_indexed_projects(self) -> list[IndexedProject]:
        """List projects indexed on this machine."""
        rows = self.db.fetchall(
            """SELECT project_id AS id, root_path, total_files, total_symbols,
                      last_indexed_at, index_duration_ms
               FROM code_indexed_project_states
               WHERE machine_id = %s
               ORDER BY last_indexed_at DESC""",
            (require_machine_id(),),
        )
        return [IndexedProject.from_row(r) for r in rows]

    def get_registry_project(self, project_id: str) -> tuple[bool, bool]:
        """Return (exists, deleted) for the hub projects row."""
        row = self.db.fetchone(
            "SELECT deleted_at FROM projects WHERE id = %s",
            (project_id,),
        )
        if row is None:
            return False, False
        return True, row["deleted_at"] is not None

    def delete_project_index(self, project_id: str) -> dict[str, int]:
        """Delete this machine's project selector while retaining shared content."""
        counts = {
            "symbols": 0,
            "files": 0,
            "imports": 0,
            "calls": 0,
            "content_chunks": 0,
        }

        def _write(conn: Transaction) -> None:
            cursor = conn.execute(
                """DELETE FROM code_indexed_project_states
                   WHERE machine_id = %s AND project_id = %s""",
                (require_machine_id(), project_id),
            )
            counts["projects"] = cursor.rowcount

        run_hub_mutation(self.db, _write)
        return counts

    def count_symbols(self, project_id: str) -> int:
        """Count total symbols for a project."""
        row = self.db.fetchone(
            """SELECT COUNT(*) AS cnt
               FROM code_symbols s
               JOIN code_indexed_file_states fs
                 ON fs.project_id = s.project_id
                AND fs.file_path = s.file_path
                AND fs.content_hash = s.file_content_hash
               WHERE fs.machine_id = %s AND fs.project_id = %s""",
            (require_machine_id(), project_id),
        )
        return cast(int, row["cnt"]) if row else 0

    def count_files(self, project_id: str) -> int:
        """Count total indexed files for a project."""
        row = self.db.fetchone(
            """SELECT COUNT(*) AS cnt FROM code_indexed_file_states
               WHERE machine_id = %s AND project_id = %s""",
            (require_machine_id(), project_id),
        )
        return cast(int, row["cnt"]) if row else 0
