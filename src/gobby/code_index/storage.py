"""SQLite CRUD for code index data.

Follows the pattern established by storage/memories.py.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from gobby.code_index.models import (
    CallRelation,
    ContentChunk,
    ImportRelation,
    IndexedFile,
    IndexedProject,
    Symbol,
)
from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


class CodeIndexStorage:
    """SQLite storage for code symbols, indexed files, and projects."""

    def __init__(self, db: DatabaseProtocol) -> None:
        self.db = db

    # ── Symbols ──────────────────────────────────────────────────────

    def upsert_symbols(self, symbols: list[Symbol]) -> int:
        """Insert or update symbols. Returns count of upserted rows."""
        if not symbols:
            return 0

        now = datetime.now(UTC).isoformat()
        rows = [
            (
                sym.id,
                sym.project_id,
                sym.file_path,
                sym.name,
                sym.qualified_name,
                sym.kind,
                sym.language,
                sym.byte_start,
                sym.byte_end,
                sym.line_start,
                sym.line_end,
                sym.signature,
                sym.docstring,
                sym.parent_symbol_id,
                sym.content_hash,
                sym.summary,
                sym.created_at,
                now,
            )
            for sym in symbols
        ]
        with self.db.transaction() as conn:
            conn.executemany(
                """INSERT INTO code_symbols (
                    id, project_id, file_path, name, qualified_name,
                    kind, language, byte_start, byte_end,
                    line_start, line_end, signature, docstring,
                    parent_symbol_id, content_hash, summary,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    qualified_name=excluded.qualified_name,
                    kind=excluded.kind,
                    byte_start=excluded.byte_start,
                    byte_end=excluded.byte_end,
                    line_start=excluded.line_start,
                    line_end=excluded.line_end,
                    signature=excluded.signature,
                    docstring=excluded.docstring,
                    parent_symbol_id=excluded.parent_symbol_id,
                    language=excluded.language,
                    content_hash=excluded.content_hash,
                    summary=CASE WHEN excluded.content_hash != code_symbols.content_hash
                                 THEN NULL ELSE code_symbols.summary END,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def get_symbol(self, symbol_id: str) -> Symbol | None:
        """Get a single symbol by ID."""
        row = self.db.fetchone("SELECT * FROM code_symbols WHERE id = ?", (symbol_id,))
        return Symbol.from_row(row) if row else None

    def get_symbols(self, symbol_ids: list[str]) -> list[Symbol]:
        """Batch-retrieve symbols by IDs."""
        if not symbol_ids:
            return []
        placeholders = ",".join("?" for _ in symbol_ids)
        rows = self.db.fetchall(
            f"SELECT * FROM code_symbols WHERE id IN ({placeholders})",
            tuple(symbol_ids),
        )
        return [Symbol.from_row(r) for r in rows]

    def get_symbols_for_file(self, project_id: str, file_path: str) -> list[Symbol]:
        """Get all symbols in a file."""
        rows = self.db.fetchall(
            "SELECT * FROM code_symbols WHERE project_id = ? AND file_path = ? ORDER BY line_start",
            (project_id, file_path),
        )
        return [Symbol.from_row(r) for r in rows]

    @staticmethod
    def _escape_like(value: str) -> str:
        """Escape SQL LIKE wildcards in user input."""
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def search_symbols_by_name(
        self,
        query: str,
        project_id: str,
        kind: str | None = None,
        file_path: str | None = None,
        limit: int = 50,
    ) -> list[Symbol]:
        """Search symbols by name prefix/substring."""
        conditions = ["project_id = ?"]
        params: list[Any] = [project_id]

        # Support both prefix and substring matching
        escaped = self._escape_like(query)
        conditions.append("(name LIKE ? ESCAPE '\\' OR qualified_name LIKE ? ESCAPE '\\')")
        params.extend([f"%{escaped}%", f"%{escaped}%"])

        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        if file_path:
            conditions.append("file_path = ?")
            params.append(file_path)

        where = " AND ".join(conditions)
        params.append(limit)

        rows = self.db.fetchall(
            f"SELECT * FROM code_symbols WHERE {where} ORDER BY name LIMIT ?",
            tuple(params),
        )
        return [Symbol.from_row(r) for r in rows]

    @staticmethod
    def _sanitize_fts_query(query: str) -> str:
        """Sanitize user input for FTS5 queries.

        Strips FTS5 special characters and joins tokens with implicit AND.
        """
        # Remove FTS5 operators and special chars
        cleaned = ""
        for ch in query:
            if ch.isalnum() or ch in (" ", "_"):
                cleaned += ch
        # Split into tokens, filter empty, join with implicit AND
        tokens = [t.strip() for t in cleaned.split() if t.strip()]
        if not tokens:
            return ""
        # Quote each token to avoid FTS5 syntax issues
        return " ".join(f'"{t}"' for t in tokens)

    def search_symbols_fts(
        self,
        query: str,
        project_id: str,
        kind: str | None = None,
        file_path: str | None = None,
        limit: int = 50,
    ) -> list[Symbol]:
        """Full-text search across symbol names, signatures, docstrings, and summaries.

        Uses FTS5 for relevance-ranked results. Falls back gracefully if the
        FTS5 table doesn't exist (pre-v155 databases).
        """
        fts_query = self._sanitize_fts_query(query)
        if not fts_query:
            return []

        conditions = ["cs.project_id = ?"]
        params: list[Any] = [project_id]

        if kind:
            conditions.append("cs.kind = ?")
            params.append(kind)
        if file_path:
            conditions.append("cs.file_path = ?")
            params.append(file_path)

        where = " AND ".join(conditions)
        params.append(limit)

        sql = f"""
            SELECT cs.* FROM code_symbols_fts fts
            JOIN code_symbols cs ON cs.rowid = fts.rowid
            WHERE code_symbols_fts MATCH ? AND {where}
            ORDER BY rank
            LIMIT ?
        """
        # MATCH param goes first
        all_params = [fts_query] + params

        try:
            rows = self.db.fetchall(sql, tuple(all_params))
            return [Symbol.from_row(r) for r in rows]
        except Exception as e:
            logger.debug(f"FTS5 search failed (table may not exist): {e}")
            return []

    def delete_symbols_for_file(self, project_id: str, file_path: str) -> int:
        """Delete all symbols for a file. Returns count."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM code_symbols WHERE project_id = ? AND file_path = ?",
                (project_id, file_path),
            )
            return cursor.rowcount

    def delete_symbols_for_project(self, project_id: str) -> int:
        """Delete all symbols for a project."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM code_symbols WHERE project_id = ?",
                (project_id,),
            )
            return cursor.rowcount

    # ── Files ────────────────────────────────────────────────────────

    def upsert_file(self, file: IndexedFile) -> None:
        """Insert or update an indexed file record."""
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO code_indexed_files (
                    id, project_id, file_path, language, content_hash,
                    symbol_count, byte_size, graph_synced, vectors_synced,
                    graph_sync_attempted_at, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    symbol_count=excluded.symbol_count,
                    byte_size=excluded.byte_size,
                    graph_synced=0,
                    graph_sync_attempted_at=NULL,
                    vectors_synced=0,
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
                    file.graph_synced,
                    file.vectors_synced,
                    file.graph_sync_attempted_at,
                    file.indexed_at,
                ),
            )

    def get_file(self, project_id: str, file_path: str) -> IndexedFile | None:
        """Get indexed file record."""
        row = self.db.fetchone(
            "SELECT * FROM code_indexed_files WHERE project_id = ? AND file_path = ?",
            (project_id, file_path),
        )
        return IndexedFile.from_row(row) if row else None

    def list_files(self, project_id: str) -> list[IndexedFile]:
        """List all indexed files for a project."""
        rows = self.db.fetchall(
            "SELECT * FROM code_indexed_files WHERE project_id = ? ORDER BY file_path",
            (project_id,),
        )
        return [IndexedFile.from_row(r) for r in rows]

    def get_stale_files(self, project_id: str, current_hashes: dict[str, str]) -> list[str]:
        """Find files whose stored hash differs from current hash.

        Uses a temp table to compare hashes in SQL, avoiding loading all
        IndexedFile objects into Python memory.

        Args:
            project_id: Project to check.
            current_hashes: Map of file_path -> current content hash.

        Returns:
            List of file paths that need re-indexing.
        """
        if not current_hashes:
            return []

        with self.db.transaction() as conn:
            conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _current_hashes "
                "(file_path TEXT PRIMARY KEY, content_hash TEXT)"
            )
            conn.execute("DELETE FROM _current_hashes")
            conn.executemany(
                "INSERT INTO _current_hashes (file_path, content_hash) VALUES (?, ?)",
                list(current_hashes.items()),
            )

            # Files that are new (not in indexed) or have changed hashes
            rows = conn.execute(
                """
                SELECT ch.file_path FROM _current_hashes ch
                LEFT JOIN code_indexed_files cf
                    ON cf.project_id = ? AND cf.file_path = ch.file_path
                WHERE cf.file_path IS NULL OR cf.content_hash != ch.content_hash
                """,
                (project_id,),
            ).fetchall()

            conn.execute("DROP TABLE IF EXISTS _current_hashes")

        return [row[0] for row in rows]

    def get_orphan_files(self, project_id: str, current_paths: set[str]) -> list[str]:
        """Find indexed files that are no longer in the candidate set.

        These are files that were previously indexed but are now excluded
        (e.g., by new exclude_patterns) or deleted from disk.

        Args:
            project_id: Project to check.
            current_paths: Set of file paths currently eligible for indexing.

        Returns:
            List of orphan file paths to clean up.
        """
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT file_path FROM code_indexed_files WHERE project_id = ?",
                (project_id,),
            ).fetchall()

        return [row[0] for row in rows if row[0] not in current_paths]

    def get_unsynced_files(self, project_id: str, limit: int = 100) -> list[IndexedFile]:
        """Get files where graph/vector sync is incomplete (graph_synced=0)."""
        rows = self.db.fetchall(
            """SELECT * FROM code_indexed_files
               WHERE project_id = ? AND graph_synced = 0
               ORDER BY indexed_at LIMIT ?""",
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
    ) -> list[IndexedFile]:
        """Get files needing external sync (vectors_synced=0 or graph_synced=0).

        Args:
            project_id: Project to query.
            limit: Max files to return.
            vectors: Include files needing vector sync.
            graph: Include files needing graph sync.
        """
        conditions = []
        if vectors:
            conditions.append("vectors_synced = 0")
        if graph:
            conditions.append("graph_synced = 0")
        if not conditions:
            return []
        where = " OR ".join(conditions)
        rows = self.db.fetchall(
            f"""SELECT * FROM code_indexed_files
                WHERE project_id = ? AND ({where})
                ORDER BY indexed_at LIMIT ?""",
            (project_id, limit),
        )
        return [IndexedFile.from_row(r) for r in rows]

    def mark_vectors_synced(self, file_id: str) -> bool:
        """Mark a file's vectors as synced. Returns True if updated."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE code_indexed_files SET vectors_synced = 1 WHERE id = ?",
                (file_id,),
            )
            return cursor.rowcount > 0

    def mark_graph_synced(self, file_id: str) -> bool:
        """Mark a file's graph edges as synced. Returns True if updated."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE code_indexed_files
                   SET graph_synced = 1, graph_sync_attempted_at = ?
                   WHERE id = ?""",
                (now, file_id),
            )
            return cursor.rowcount > 0

    def mark_graph_sync_attempted(self, file_id: str) -> bool:
        """Mark that a graph sync was attempted, even if it later fails."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE code_indexed_files
                   SET graph_synced = 0, graph_sync_attempted_at = ?
                   WHERE id = ?""",
                (now, file_id),
            )
            return cursor.rowcount > 0

    def reset_graph_sync_for_project(self, project_id: str) -> int:
        """Mark every file in a project as needing graph rebuild."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE code_indexed_files
                   SET graph_synced = 0, graph_sync_attempted_at = NULL
                   WHERE project_id = ?""",
                (project_id,),
            )
            return cursor.rowcount

    # ── Imports & Calls ─────────────────────────────────────────────

    def upsert_imports(
        self,
        project_id: str,
        file_path: str,
        imports: list[ImportRelation],
    ) -> int:
        """Replace import relations for a file. Returns count inserted."""
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM code_imports WHERE project_id = ? AND source_file = ?",
                (project_id, file_path),
            )
            if not imports:
                return 0
            conn.executemany(
                """INSERT OR IGNORE INTO code_imports
                   (project_id, source_file, target_module)
                   VALUES (?, ?, ?)""",
                [(project_id, imp.source_file, imp.target_module) for imp in imports],
            )
            return len(imports)

    def upsert_calls(
        self,
        project_id: str,
        file_path: str,
        calls: list[CallRelation],
    ) -> int:
        """Replace call relations for a file. Returns count inserted."""
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM code_calls WHERE project_id = ? AND file_path = ?",
                (project_id, file_path),
            )
            if not calls:
                return 0
            conn.executemany(
                """INSERT OR IGNORE INTO code_calls
                   (
                       project_id, caller_symbol_id, callee_symbol_id, callee_name,
                       callee_target_kind, callee_external_module, file_path, line
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        project_id,
                        c.caller_symbol_id,
                        # SQLite stores optional callee_* fields as empty strings;
                        # the read path normalizes those empty strings back to None.
                        c.callee_symbol_id or "",
                        c.callee_name,
                        c.callee_target_kind,
                        c.callee_external_module or "",
                        c.file_path,
                        c.line,
                    )
                    for c in calls
                ],
            )
            return len(calls)

    def get_imports_for_file(self, project_id: str, file_path: str) -> list[dict[str, Any]]:
        """Get import relations for a file (for graph sync)."""
        rows = self.db.fetchall(
            """SELECT source_file, target_module FROM code_imports
               WHERE project_id = ? AND source_file = ?""",
            (project_id, file_path),
        )
        return [
            {"source_file": r["source_file"], "target_module": r["target_module"]} for r in rows
        ]

    def get_calls_for_file(self, project_id: str, file_path: str) -> list[dict[str, Any]]:
        """Get call relations for a file (for graph sync)."""
        rows = self.db.fetchall(
            """SELECT caller_symbol_id, callee_symbol_id, callee_name, callee_target_kind,
                      callee_external_module, file_path, line
               FROM code_calls
               WHERE project_id = ? AND file_path = ?""",
            (project_id, file_path),
        )
        return [
            {
                "caller_symbol_id": r["caller_symbol_id"],
                "callee_symbol_id": r["callee_symbol_id"] or None,
                "callee_name": r["callee_name"],
                "callee_target_kind": r["callee_target_kind"],
                "callee_external_module": r["callee_external_module"] or None,
                "file_path": r["file_path"],
                "line": r["line"],
            }
            for r in rows
        ]

    def delete_imports_for_file(self, project_id: str, file_path: str) -> int:
        """Delete import relations for a file. Returns count deleted."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM code_imports WHERE project_id = ? AND source_file = ?",
                (project_id, file_path),
            )
            return cursor.rowcount

    def delete_calls_for_file(self, project_id: str, file_path: str) -> int:
        """Delete call relations for a file. Returns count deleted."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM code_calls WHERE project_id = ? AND file_path = ?",
                (project_id, file_path),
            )
            return cursor.rowcount

    def delete_file(self, project_id: str, file_path: str) -> None:
        """Delete a file record (symbols deleted separately)."""
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM code_indexed_files WHERE project_id = ? AND file_path = ?",
                (project_id, file_path),
            )

    def delete_files_for_project(self, project_id: str) -> int:
        """Delete all file records for a project. Returns count."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM code_indexed_files WHERE project_id = ?",
                (project_id,),
            )
            return cursor.rowcount

    # ── Projects ─────────────────────────────────────────────────────

    def upsert_project_stats(self, project: IndexedProject) -> None:
        """Insert or update project statistics."""
        with self.db.transaction() as conn:
            conn.execute(
                """INSERT INTO code_indexed_projects (
                    id, root_path, total_files, total_symbols,
                    last_indexed_at, index_duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    total_files=excluded.total_files,
                    total_symbols=excluded.total_symbols,
                    last_indexed_at=excluded.last_indexed_at,
                    index_duration_ms=excluded.index_duration_ms,
                    updated_at=datetime('now')
                """,
                (
                    project.id,
                    project.root_path,
                    project.total_files,
                    project.total_symbols,
                    project.last_indexed_at,
                    project.index_duration_ms,
                ),
            )

    def get_project_stats(self, project_id: str) -> IndexedProject | None:
        """Get project statistics."""
        row = self.db.fetchone(
            "SELECT * FROM code_indexed_projects WHERE id = ?",
            (project_id,),
        )
        return IndexedProject.from_row(row) if row else None

    def list_indexed_projects(self) -> list[IndexedProject]:
        """List all indexed projects."""
        rows = self.db.fetchall("SELECT * FROM code_indexed_projects ORDER BY last_indexed_at DESC")
        return [IndexedProject.from_row(r) for r in rows]

    # ── Summaries ────────────────────────────────────────────────────

    def get_unsummarized_symbols(
        self,
        project_id: str,
        kinds: list[str] | None = None,
        limit: int = 20,
    ) -> list[Symbol]:
        """Get symbols that have no summary yet.

        Args:
            project_id: Project to query.
            kinds: Symbol kinds to include (default: function, class, method).
            limit: Max symbols to return.
        """
        if kinds is None:
            kinds = ["function", "class", "method"]
        placeholders = ",".join("?" for _ in kinds)
        rows = self.db.fetchall(
            f"""SELECT * FROM code_symbols
                WHERE project_id = ? AND summary IS NULL
                  AND kind IN ({placeholders})
                ORDER BY updated_at DESC
                LIMIT ?""",
            (project_id, *kinds, limit),
        )
        return [Symbol.from_row(r) for r in rows]

    def update_symbol_summary(self, symbol_id: str, summary: str) -> bool:
        """Set the summary for a symbol. Returns True if updated."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE code_symbols SET summary = ? WHERE id = ?",
                (summary, symbol_id),
            )
            return cursor.rowcount > 0

    # ── Counts ───────────────────────────────────────────────────────

    def count_symbols(self, project_id: str) -> int:
        """Count total symbols for a project."""
        row = self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM code_symbols WHERE project_id = ?",
            (project_id,),
        )
        return row["cnt"] if row else 0

    def count_files(self, project_id: str) -> int:
        """Count total indexed files for a project."""
        row = self.db.fetchone(
            "SELECT COUNT(*) as cnt FROM code_indexed_files WHERE project_id = ?",
            (project_id,),
        )
        return row["cnt"] if row else 0

    # ── Content Chunks ──────────────────────────────────────────────

    def upsert_content_chunks(self, chunks: list[ContentChunk]) -> int:
        """Insert or update content chunks. Returns count of upserted rows."""
        if not chunks:
            return 0

        rows = [
            (
                chunk.id,
                chunk.project_id,
                chunk.file_path,
                chunk.chunk_index,
                chunk.line_start,
                chunk.line_end,
                chunk.content,
                chunk.language,
                chunk.created_at,
            )
            for chunk in chunks
        ]
        with self.db.transaction() as conn:
            conn.executemany(
                """INSERT INTO code_content_chunks (
                    id, project_id, file_path, chunk_index,
                    line_start, line_end, content, language, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    content = excluded.content,
                    line_start = excluded.line_start,
                    line_end = excluded.line_end
                """,
                rows,
            )
        return len(rows)

    def delete_content_chunks_for_file(self, project_id: str, file_path: str) -> None:
        """Delete all content chunks for a file."""
        self.db.execute(
            "DELETE FROM code_content_chunks WHERE project_id = ? AND file_path = ?",
            (project_id, file_path),
        )

    def delete_content_chunks_for_project(self, project_id: str) -> None:
        """Delete all content chunks for a project."""
        self.db.execute(
            "DELETE FROM code_content_chunks WHERE project_id = ?",
            (project_id,),
        )

    # ── Graph visualization fallbacks ────────────────────────────────

    def get_file_symbol_tree(self, project_id: str, limit: int = 200) -> dict[str, Any]:
        """Build file→symbol containment graph from SQLite.

        Fallback for when Neo4j is unavailable. No call/import edges,
        but still browsable as a file-to-symbol tree.
        """
        file_rows = self.db.fetchall(
            """SELECT f.file_path, f.language, f.symbol_count
               FROM code_indexed_files f
               WHERE f.project_id = ?
               ORDER BY
                 CASE WHEN f.language IN ('markdown','yaml','json')
                      THEN 1 ELSE 0 END,
                 f.symbol_count DESC, f.file_path
               LIMIT ?""",
            (project_id, limit),
        )

        nodes: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        file_paths = []

        for row in file_rows:
            fp = row["file_path"]
            file_paths.append(fp)
            nodes.append(
                {
                    "id": fp,
                    "name": fp,
                    "type": "file",
                    "file_path": fp,
                    "language": row["language"],
                    "symbol_count": row["symbol_count"] or 0,
                }
            )

        # Get top-level symbols for each file (limit to avoid explosion)
        if file_paths:
            placeholders = ",".join("?" for _ in file_paths)
            sym_rows = self.db.fetchall(
                f"""SELECT id, name, kind, file_path, line_start, signature
                    FROM code_symbols
                    WHERE project_id = ? AND file_path IN ({placeholders})
                      AND parent_symbol_id IS NULL
                    ORDER BY file_path, line_start""",
                (project_id, *file_paths),
            )
            for row in sym_rows:
                nodes.append(
                    {
                        "id": row["id"],
                        "name": row["name"],
                        "type": row["kind"] or "function",
                        "kind": row["kind"],
                        "file_path": row["file_path"],
                        "line_start": row["line_start"],
                        "signature": row["signature"],
                    }
                )
                links.append(
                    {
                        "source": row["file_path"],
                        "target": row["id"],
                        "type": "DEFINES",
                    }
                )

        return {"nodes": nodes, "links": links}

    def search_symbols_for_graph(
        self, query: str, project_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Search symbols and return in graph-node format.

        Uses existing FTS and name search, returns results formatted
        for graph visualization.
        """
        # Try FTS first, fall back to name search
        symbols = self.search_symbols_fts(query, project_id, limit=limit)
        if not symbols:
            symbols = self.search_symbols_by_name(query, project_id, limit=limit)

        return [
            {
                "id": sym.id,
                "name": sym.name,
                "type": sym.kind or "function",
                "kind": sym.kind,
                "file_path": sym.file_path,
                "line_start": sym.line_start,
                "signature": sym.signature,
            }
            for sym in symbols
        ]

    def search_content_fts(
        self,
        query: str,
        project_id: str,
        file_path: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Full-text search across file content chunks.

        Returns dicts with file_path, line_start, line_end, snippet, language.
        """
        if not query.strip():
            return []

        # Escape FTS5 special characters for safe querying
        safe_query = query.replace('"', '""')

        try:
            if file_path:
                rows = self.db.fetchall(
                    """SELECT
                        c.file_path, c.line_start, c.line_end, c.language,
                        snippet(code_content_fts, 0, '>>>', '<<<', '...', 40) as snippet
                    FROM code_content_fts fts
                    JOIN code_content_chunks c ON c.rowid = fts.rowid
                    WHERE code_content_fts MATCH ?
                      AND c.project_id = ?
                      AND c.file_path = ?
                    ORDER BY rank
                    LIMIT ?""",
                    (f'"{safe_query}"', project_id, file_path, limit),
                )
            else:
                rows = self.db.fetchall(
                    """SELECT
                        c.file_path, c.line_start, c.line_end, c.language,
                        snippet(code_content_fts, 0, '>>>', '<<<', '...', 40) as snippet
                    FROM code_content_fts fts
                    JOIN code_content_chunks c ON c.rowid = fts.rowid
                    WHERE code_content_fts MATCH ?
                      AND c.project_id = ?
                    ORDER BY rank
                    LIMIT ?""",
                    (f'"{safe_query}"', project_id, limit),
                )
        except Exception as e:
            logger.debug(f"Content FTS search failed, falling back to LIKE: {e}")
            # Fallback to LIKE search
            like_query = f"%{query}%"
            params: list[Any] = [project_id, like_query]
            sql = """SELECT file_path, line_start, line_end, language,
                        substr(content, max(1, instr(content, ?) - 60), 120) as snippet
                     FROM code_content_chunks
                     WHERE project_id = ? AND content LIKE ?"""
            if file_path:
                sql += " AND file_path = ?"
                params = [query, project_id, like_query, file_path]
            else:
                params = [query, project_id, like_query]
            sql += " LIMIT ?"
            params.append(limit)
            rows = self.db.fetchall(sql, tuple(params))

        return [
            {
                "file_path": row["file_path"],
                "line_start": row["line_start"],
                "line_end": row["line_end"],
                "snippet": row["snippet"],
                "language": row["language"],
            }
            for row in rows
        ]
