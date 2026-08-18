"""Content chunk storage and content search helpers."""

from __future__ import annotations

import logging
from typing import Any

from gobby.code_index._storage.search_helpers import make_snippet, rows_by_ids
from gobby.code_index.models import ContentChunk
from gobby.search import keyword
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.machine_id import require_machine_id

logger = logging.getLogger(__name__)


class CodeIndexContentStorageMixin:
    """Storage methods for file content chunks and content search."""

    db: HubDatabase

    def upsert_content_chunks(self, chunks: list[ContentChunk]) -> int:
        """Insert or update content chunks. Returns count of upserted rows."""
        if not chunks:
            return 0

        rows = [
            (
                chunk.id,
                chunk.project_id,
                chunk.file_path,
                chunk.content_hash,
                chunk.chunk_index,
                chunk.line_start,
                chunk.line_end,
                chunk.content,
                chunk.language,
            )
            for chunk in chunks
        ]
        with self.db.transaction() as conn:
            conn.executemany(
                """INSERT INTO code_content_chunks (
                    id, project_id, file_path, content_hash, chunk_index,
                    line_start, line_end, content, language
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(id) DO UPDATE SET
                    content = excluded.content,
                    line_start = excluded.line_start,
                    line_end = excluded.line_end
                """,
                rows,
            )
        return len(rows)

    def search_content_fts(
        self,
        query: str,
        project_id: str,
        file_path: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Full-text search across file content chunks."""
        if not query.strip():
            return []

        try:
            hits = keyword.pick_search_backend(self.db, "code_content").search(
                query,
                limit * 4,
                filters={"project_id": project_id, "file_path": file_path},
            )
            rows = rows_by_ids(self.db, "code_content_chunks", [hit.id for hit in hits])
            states = self.db.fetchall(
                """SELECT file_path, content_hash FROM code_indexed_file_states
                   WHERE machine_id = %s AND project_id = %s""",
                (require_machine_id(), project_id),
            )
        except Exception as exc:
            logger.warning("Code content keyword search failed: %s", exc, exc_info=True)
            return []

        rows_by_id = {str(row["id"]): row for row in rows}
        visible_versions = {(row["file_path"], row["content_hash"]) for row in states}
        results = [
            {
                "file_path": row["file_path"],
                "line_start": row["line_start"],
                "line_end": row["line_end"],
                "snippet": make_snippet(row["content"], query),
                "language": row["language"],
            }
            for hit in hits
            if (row := rows_by_id.get(hit.id)) is not None
            and (row["file_path"], row["content_hash"]) in visible_versions
        ]
        return results[:limit]
