"""Indexed file storage, stale-file detection, and sync marker helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.code_index._storage.constants import SYNC_FAILURE_COOLOFF_SECONDS
from gobby.code_index.models import IndexedFile
from gobby.storage.hub.protocol import HubDatabase


class CodeIndexFileStorageMixin:
    """Storage methods for indexed files and external sync state."""

    db: HubDatabase

    def upsert_file(self, file: IndexedFile) -> None:
        """Reference Python file writer used by tests; production indexing is Rust gcode."""
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO code_indexed_files (
                    id, project_id, file_path, language, content_hash,
                    symbol_count, byte_size, graph_synced, vectors_synced,
                    graph_sync_attempted_at, vector_sync_attempted_at, indexed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(id) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    symbol_count=excluded.symbol_count,
                    byte_size=excluded.byte_size,
                    graph_synced=FALSE,
                    graph_sync_attempted_at=NULL,
                    vectors_synced=FALSE,
                    vector_sync_attempted_at=NULL,
                    indexed_at=excluded.indexed_at
                """,
                (
                    file.id,
                    file.project_id,
                    file.file_path,
                    file.language,
                    file.content_hash,
                    file.symbol_count,
                    file.byte_size,
                    bool(file.graph_synced),
                    bool(file.vectors_synced),
                    file.graph_sync_attempted_at,
                    file.vector_sync_attempted_at,
                    file.indexed_at,
                ),
            )

    def get_file(self, project_id: str, file_path: str) -> IndexedFile | None:
        """Get indexed file record."""
        row = self.db.fetchone(
            "SELECT * FROM code_indexed_files WHERE project_id = %s AND file_path = %s",
            (project_id, file_path),
        )
        return IndexedFile.from_row(row) if row else None

    def list_files(self, project_id: str) -> list[IndexedFile]:
        """List all indexed files for a project."""
        rows = self.db.fetchall(
            "SELECT * FROM code_indexed_files WHERE project_id = %s ORDER BY file_path",
            (project_id,),
        )
        return [IndexedFile.from_row(r) for r in rows]

    def get_stale_files(self, project_id: str, current_hashes: dict[str, str]) -> list[str]:
        """Find files whose stored hash differs from current hash."""
        if not current_hashes:
            return []

        with self.db.transaction() as conn:
            conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _current_hashes "
                "(file_path TEXT PRIMARY KEY, content_hash TEXT)"
            )
            conn.execute("DELETE FROM _current_hashes")
            conn.executemany(
                "INSERT INTO _current_hashes (file_path, content_hash) VALUES (%s, %s)",
                list(current_hashes.items()),
            )

            rows = conn.execute(
                """
                SELECT ch.file_path AS file_path FROM _current_hashes ch
                LEFT JOIN code_indexed_files cf
                    ON cf.project_id = %s AND cf.file_path = ch.file_path
                WHERE cf.file_path IS NULL OR cf.content_hash != ch.content_hash
                """,
                (project_id,),
            ).fetchall()

            conn.execute("DROP TABLE IF EXISTS _current_hashes")

        return [row["file_path"] for row in rows]

    def get_orphan_files(self, project_id: str, current_paths: set[str]) -> list[str]:
        """Find indexed files that are no longer in the candidate set."""
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT file_path AS file_path FROM code_indexed_files WHERE project_id = %s",
                (project_id,),
            ).fetchall()

        return [row["file_path"] for row in rows if row["file_path"] not in current_paths]

    def get_unsynced_files(self, project_id: str, limit: int = 100) -> list[IndexedFile]:
        """Get files where graph/vector sync is incomplete."""
        rows = self.db.fetchall(
            """SELECT * FROM code_indexed_files
               WHERE project_id = %s AND graph_synced IS FALSE
               ORDER BY indexed_at LIMIT %s""",
            (project_id, limit),
        )
        return [IndexedFile.from_row(r) for r in rows]

    def get_pending_sync_files(
        self,
        project_id: str,
        limit: int = 50,
        *,
        vectors: bool = True,
        graph: bool = True,
        failure_cooloff_seconds: int = SYNC_FAILURE_COOLOFF_SECONDS,
    ) -> list[IndexedFile]:
        """Get files needing external sync."""
        retry_cutoff = (datetime.now(UTC) - timedelta(seconds=failure_cooloff_seconds)).isoformat()
        conditions = []
        params: list[Any] = [project_id]
        if vectors:
            conditions.append(
                """(vectors_synced IS FALSE
                   AND (vector_sync_attempted_at IS NULL OR vector_sync_attempted_at < %s))"""
            )
            params.append(retry_cutoff)
        if graph:
            conditions.append(
                """(graph_synced IS FALSE
                   AND (graph_sync_attempted_at IS NULL OR graph_sync_attempted_at < %s))"""
            )
            params.append(retry_cutoff)
        if not conditions:
            return []
        where = " OR ".join(conditions)
        params.append(limit)
        rows = self.db.fetchall(
            f"""SELECT * FROM code_indexed_files
                WHERE project_id = %s AND ({where})
                ORDER BY indexed_at LIMIT %s""",
            tuple(params),
        )
        return [IndexedFile.from_row(r) for r in rows]

    def mark_vectors_synced(self, file_id: str, content_hash: str) -> bool:
        """Mark a file's vectors as synced if the content hash still matches."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE code_indexed_files
                   SET vectors_synced = TRUE, vector_sync_attempted_at = %s
                   WHERE id = %s AND content_hash = %s""",
                (datetime.now(UTC).isoformat(), file_id, content_hash),
            )
            return cursor.rowcount > 0

    def mark_vector_sync_attempted(self, file_id: str) -> bool:
        """Mark that a vector sync was attempted, even if it later fails."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE code_indexed_files
                   SET vectors_synced = FALSE, vector_sync_attempted_at = %s
                   WHERE id = %s""",
                (now, file_id),
            )
            return cursor.rowcount > 0

    def mark_graph_synced(self, file_id: str, content_hash: str) -> bool:
        """Mark a file's graph edges as synced if the content hash still matches."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE code_indexed_files
                   SET graph_synced = TRUE, graph_sync_attempted_at = %s
                   WHERE id = %s AND content_hash = %s""",
                (now, file_id, content_hash),
            )
            return cursor.rowcount > 0

    def mark_graph_sync_attempted(self, file_id: str) -> bool:
        """Mark that a graph sync was attempted, even if it later fails."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE code_indexed_files
                   SET graph_synced = FALSE, graph_sync_attempted_at = %s
                   WHERE id = %s""",
                (now, file_id),
            )
            return cursor.rowcount > 0

    def reset_graph_sync_for_project(self, project_id: str) -> int:
        """Mark every file in a project as needing graph rebuild."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE code_indexed_files
                   SET graph_synced = FALSE, graph_sync_attempted_at = NULL
                   WHERE project_id = %s""",
                (project_id,),
            )
            return cursor.rowcount

    def delete_file(self, project_id: str, file_path: str) -> None:
        """Delete a file record (symbols deleted separately)."""
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM code_indexed_files WHERE project_id = %s AND file_path = %s",
                (project_id, file_path),
            )

    def delete_files_for_project(self, project_id: str) -> int:
        """Delete all file records for a project. Returns count."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM code_indexed_files WHERE project_id = %s",
                (project_id,),
            )
            return cursor.rowcount
