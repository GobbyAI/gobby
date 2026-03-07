"""SQLite CRUD for code index data.

Follows the pattern established by storage/memories.py.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from gobby.code_index.models import IndexedFile, IndexedProject, Symbol
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
                    content_hash=excluded.content_hash,
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
                    symbol_count, byte_size, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    symbol_count=excluded.symbol_count,
                    byte_size=excluded.byte_size,
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

        Args:
            project_id: Project to check.
            current_hashes: Map of file_path -> current content hash.

        Returns:
            List of file paths that need re-indexing.
        """
        stored = self.list_files(project_id)
        stale: list[str] = []
        stored_map = {f.file_path: f.content_hash for f in stored}

        for path, current_hash in current_hashes.items():
            stored_hash = stored_map.get(path)
            if stored_hash is None or stored_hash != current_hash:
                stale.append(path)

        return stale

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

    def update_symbol_summary(self, symbol_id: str, summary: str) -> None:
        """Set AI-generated summary for a symbol."""
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE code_symbols SET summary = ?, updated_at = datetime('now') WHERE id = ?",
                (summary, symbol_id),
            )

    def get_symbols_without_summaries(self, project_id: str, limit: int = 50) -> list[Symbol]:
        """Get symbols that need summaries generated."""
        rows = self.db.fetchall(
            """SELECT * FROM code_symbols
               WHERE project_id = ? AND summary IS NULL
               ORDER BY kind, name LIMIT ?""",
            (project_id, limit),
        )
        return [Symbol.from_row(r) for r in rows]

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
