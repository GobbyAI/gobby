"""Symbol storage and symbol search helpers."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from gobby.code_index._storage.search_helpers import rows_by_ids
from gobby.code_index.models import Symbol
from gobby.search import keyword
from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


class CodeIndexSymbolStorageMixin:
    """Storage methods for symbols and symbol search."""

    db: HubDatabase

    def upsert_symbols(self, symbols: list[Symbol]) -> int:
        """Reference Python symbol writer used by tests; production indexing is Rust gcode."""
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
                sym.summary_attempted_at,
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
                    summary_attempted_at, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                    summary_attempted_at=CASE WHEN excluded.content_hash != code_symbols.content_hash
                                             THEN NULL ELSE code_symbols.summary_attempted_at END,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
        return len(rows)

    def get_symbol(self, symbol_id: str) -> Symbol | None:
        """Get a single symbol by ID."""
        row = self.db.fetchone("SELECT * FROM code_symbols WHERE id = %s", (symbol_id,))
        return Symbol.from_row(row) if row else None

    def get_symbols(self, symbol_ids: list[str]) -> list[Symbol]:
        """Batch-retrieve symbols by IDs."""
        if not symbol_ids:
            return []
        placeholders = ",".join("%s" for _ in symbol_ids)
        rows = self.db.fetchall(
            f"SELECT * FROM code_symbols WHERE id IN ({placeholders})",
            tuple(symbol_ids),
        )
        return [Symbol.from_row(r) for r in rows]

    def get_symbols_for_file(self, project_id: str, file_path: str) -> list[Symbol]:
        """Get all symbols in a file."""
        rows = self.db.fetchall(
            "SELECT * FROM code_symbols WHERE project_id = %s AND file_path = %s ORDER BY line_start",
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
        conditions = ["project_id = %s"]
        params: list[Any] = [project_id]

        escaped = self._escape_like(query)
        conditions.append("(name LIKE %s ESCAPE '\\' OR qualified_name LIKE %s ESCAPE '\\')")
        params.extend([f"%{escaped}%", f"%{escaped}%"])

        if kind:
            conditions.append("kind = %s")
            params.append(kind)
        if file_path:
            conditions.append("file_path = %s")
            params.append(file_path)

        where = " AND ".join(conditions)
        params.extend([query, f"{escaped}%", limit])

        rows = self.db.fetchall(
            f"""
            SELECT *
            FROM code_symbols
            WHERE {where}
            ORDER BY (name = %s) DESC, (name LIKE %s ESCAPE '\\') DESC, name, id
            LIMIT %s
            """,
            tuple(params),
        )
        return [Symbol.from_row(r) for r in rows]

    def search_symbols_fts(
        self,
        query: str,
        project_id: str,
        kind: str | None = None,
        file_path: str | None = None,
        limit: int = 50,
    ) -> list[Symbol]:
        """Full-text search across symbol names, signatures, docstrings, and summaries."""
        if not query.strip():
            return []

        try:
            hits = keyword.pick_search_backend(self.db, "code_symbols").search(
                query,
                limit,
                filters={"project_id": project_id, "kind": kind, "file_path": file_path},
            )
            rows = rows_by_ids(self.db, "code_symbols", [hit.id for hit in hits])
        except Exception as exc:
            logger.debug("Code symbol keyword search failed: %s", exc, exc_info=True)
            return []
        symbols_by_id = {str(row["id"]): Symbol.from_row(row) for row in rows}
        return [symbols_by_id[hit.id] for hit in hits if hit.id in symbols_by_id]

    def delete_symbols_for_file(self, project_id: str, file_path: str) -> int:
        """Delete all symbols for a file. Returns count."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM code_symbols WHERE project_id = %s AND file_path = %s",
                (project_id, file_path),
            )
            return cursor.rowcount

    def delete_symbols_for_project(self, project_id: str) -> int:
        """Delete all symbols for a project."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM code_symbols WHERE project_id = %s",
                (project_id,),
            )
            return cursor.rowcount

    def search_symbols_for_graph(
        self, query: str, project_id: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Search symbols and return in graph-node format."""
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
