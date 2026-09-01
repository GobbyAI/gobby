"""Indexed file storage, stale-file detection, and sync marker helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from gobby.code_index._storage.constants import SYNC_FAILURE_COOLOFF_SECONDS
from gobby.code_index.models import IndexedFile, IndexWriteMode
from gobby.storage.hub.protocol import HubDatabase, Transaction
from gobby.utils.machine_id import require_machine_id


class CodeIndexFileStorageMixin:
    """Storage methods for indexed files and external sync state."""

    db: HubDatabase

    @staticmethod
    def _lock_primary_checkout(
        conn: Transaction,
        machine_id: str,
        project_id: str,
        root_path: str,
        mode: IndexWriteMode,
    ) -> None:
        if mode is IndexWriteMode.OVERLAY:
            return
        checkout = conn.execute(
            """SELECT 1 FROM project_checkouts
               WHERE machine_id = %s AND project_id = %s AND root_path = %s
               FOR SHARE""",
            (machine_id, project_id, root_path),
        ).fetchone()
        if checkout is None:
            raise ValueError(f"primary index checkout mismatch for project {project_id}")

    def upsert_file(
        self,
        file: IndexedFile,
        *,
        root_path: str,
        mode: IndexWriteMode,
    ) -> None:
        """Reference Python file writer used by tests; production indexing is Rust gcode."""
        machine_id = require_machine_id()
        with self.db.transaction() as conn:
            self._lock_primary_checkout(conn, machine_id, file.project_id, root_path, mode)
            conn.execute(
                """INSERT INTO code_indexed_files (
                    id, project_id, file_path, language, content_hash,
                    symbol_count, byte_size, graph_synced, vectors_synced,
                    graph_sync_attempted_at, vector_sync_attempted_at, indexed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(id) DO UPDATE SET
                    language=excluded.language,
                    symbol_count=excluded.symbol_count,
                    byte_size=excluded.byte_size,
                    indexed_at=excluded.indexed_at,
                    last_referenced_at=NOW()
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
            conn.execute(
                """INSERT INTO code_indexed_file_states
                       (machine_id, project_id, file_path, content_hash)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT(machine_id, project_id, file_path) DO UPDATE SET
                       content_hash=excluded.content_hash,
                       updated_at=CURRENT_TIMESTAMP""",
                (machine_id, file.project_id, file.file_path, file.content_hash),
            )

    def get_file(self, project_id: str, file_path: str) -> IndexedFile | None:
        """Get indexed file record."""
        row = self.db.fetchone(
            """SELECT f.* FROM code_indexed_file_states fs
               JOIN code_indexed_files f
                 ON f.project_id = fs.project_id
                AND f.file_path = fs.file_path
                AND f.content_hash = fs.content_hash
               WHERE fs.machine_id = %s AND fs.project_id = %s AND fs.file_path = %s""",
            (require_machine_id(), project_id, file_path),
        )
        return IndexedFile.from_row(row) if row else None

    def list_files(self, project_id: str) -> list[IndexedFile]:
        """List all indexed files for a project."""
        rows = self.db.fetchall(
            """SELECT f.* FROM code_indexed_file_states fs
               JOIN code_indexed_files f
                 ON f.project_id = fs.project_id
                AND f.file_path = fs.file_path
                AND f.content_hash = fs.content_hash
               WHERE fs.machine_id = %s AND fs.project_id = %s
               ORDER BY fs.file_path""",
            (require_machine_id(), project_id),
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
                LEFT JOIN code_indexed_file_states fs
                    ON fs.machine_id = %s AND fs.project_id = %s AND fs.file_path = ch.file_path
                WHERE fs.file_path IS NULL OR fs.content_hash != ch.content_hash
                """,
                (require_machine_id(), project_id),
            ).fetchall()

            conn.execute("DROP TABLE IF EXISTS _current_hashes")

        return [row["file_path"] for row in rows]

    def get_orphan_files(self, project_id: str, current_paths: set[str]) -> list[str]:
        """Find indexed files that are no longer in the candidate set."""
        with self.db.transaction() as conn:
            rows = conn.execute(
                """SELECT file_path FROM code_indexed_file_states
                   WHERE machine_id = %s AND project_id = %s""",
                (require_machine_id(), project_id),
            ).fetchall()

        return [row["file_path"] for row in rows if row["file_path"] not in current_paths]

    def get_unsynced_files(self, project_id: str, limit: int = 100) -> list[IndexedFile]:
        """Get files where graph/vector sync is incomplete."""
        rows = self.db.fetchall(
            """SELECT f.* FROM code_indexed_file_states fs
               JOIN code_indexed_files f
                 ON f.project_id = fs.project_id
                AND f.file_path = fs.file_path
                AND f.content_hash = fs.content_hash
               WHERE fs.machine_id = %s AND fs.project_id = %s
                 AND f.graph_synced IS FALSE
               ORDER BY f.indexed_at LIMIT %s""",
            (require_machine_id(), project_id, limit),
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
        params: list[Any] = [require_machine_id(), project_id]
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
            f"""SELECT f.* FROM code_indexed_file_states fs
                JOIN code_indexed_files f
                  ON f.project_id = fs.project_id
                 AND f.file_path = fs.file_path
                 AND f.content_hash = fs.content_hash
                WHERE fs.machine_id = %s AND fs.project_id = %s AND ({where})
                ORDER BY f.indexed_at LIMIT %s""",
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
                """UPDATE code_indexed_files f
                   SET graph_synced = FALSE, graph_sync_attempted_at = NULL
                   FROM code_indexed_file_states fs
                   WHERE fs.machine_id = %s AND fs.project_id = %s
                     AND f.project_id = fs.project_id
                     AND f.file_path = fs.file_path
                     AND f.content_hash = fs.content_hash""",
                (require_machine_id(), project_id),
            )
            return cursor.rowcount

    def delete_file(
        self,
        project_id: str,
        file_path: str,
        *,
        root_path: str,
        mode: IndexWriteMode,
    ) -> None:
        """Delete this machine's selector for a file."""
        machine_id = require_machine_id()
        with self.db.transaction() as conn:
            self._lock_primary_checkout(conn, machine_id, project_id, root_path, mode)
            conn.execute(
                """DELETE FROM code_indexed_file_states
                   WHERE machine_id = %s AND project_id = %s AND file_path = %s""",
                (machine_id, project_id, file_path),
            )
