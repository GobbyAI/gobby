"""Symbol storage and symbol search helpers."""

from __future__ import annotations

import logging
from typing import Any

from gobby.code_index._storage.search_helpers import rows_by_ids
from gobby.code_index.models import Symbol
from gobby.search import keyword
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.machine_id import require_machine_id

logger = logging.getLogger(__name__)

SYMBOL_SEARCH_OVERFETCH_FACTOR = 4


class CodeIndexSymbolStorageMixin:
    """Storage methods for symbols and symbol search."""

    db: HubDatabase

    def upsert_symbols(self, symbols: list[Symbol]) -> int:
        """Reference Python symbol writer used by tests; production indexing is Rust gcode."""
        if not symbols:
            return 0

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
                sym.file_content_hash,
                sym.content_hash,
                sym.summary,
                sym.summary_attempted_at,
            )
            for sym in symbols
        ]
        with self.db.transaction() as conn:
            conn.executemany(
                """INSERT INTO code_symbols (
                    id, project_id, file_path, name, qualified_name,
                    kind, language, byte_start, byte_end,
                    line_start, line_end, signature, docstring,
                    parent_symbol_id, file_content_hash, content_hash, summary,
                    summary_attempted_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s
                )
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
                    file_content_hash=excluded.file_content_hash,
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
            """SELECT s.* FROM code_symbols s
               JOIN code_indexed_file_states fs
                 ON fs.project_id = s.project_id
                AND fs.file_path = s.file_path
                AND fs.content_hash = s.file_content_hash
               WHERE fs.machine_id = %s AND fs.project_id = %s AND fs.file_path = %s
               ORDER BY s.line_start""",
            (require_machine_id(), project_id, file_path),
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
        conditions = ["fs.machine_id = %s", "s.project_id = %s"]
        params: list[Any] = [require_machine_id(), project_id]

        escaped = self._escape_like(query)
        conditions.append("(s.name LIKE %s ESCAPE '\\' OR s.qualified_name LIKE %s ESCAPE '\\')")
        params.extend([f"%{escaped}%", f"%{escaped}%"])

        if kind:
            conditions.append("s.kind = %s")
            params.append(kind)
        if file_path:
            conditions.append("s.file_path = %s")
            params.append(file_path)

        where = " AND ".join(conditions)
        params.extend([query, f"{escaped}%", limit])

        rows = self.db.fetchall(
            f"""
            SELECT s.*
            FROM code_symbols s
            JOIN code_indexed_file_states fs
              ON fs.project_id = s.project_id
             AND fs.file_path = s.file_path
             AND fs.content_hash = s.file_content_hash
            WHERE {where}
            ORDER BY (s.name = %s) DESC, (s.name LIKE %s ESCAPE '\\') DESC, s.name, s.id
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
                limit * SYMBOL_SEARCH_OVERFETCH_FACTOR,
                filters={"project_id": project_id, "kind": kind, "file_path": file_path},
            )
        except Exception as exc:
            logger.debug("Code symbol keyword search failed: %s", exc, exc_info=True)
            return []

        rows = rows_by_ids(self.db, "code_symbols", [hit.id for hit in hits])
        if not rows:
            return []
        hit_paths = sorted({str(row["file_path"]) for row in rows})
        states = self.db.fetchall(
            """SELECT file_path, content_hash FROM code_indexed_file_states
               WHERE machine_id = %s AND project_id = %s AND file_path = ANY(%s)""",
            (require_machine_id(), project_id, hit_paths),
        )
        symbols_by_id = {str(row["id"]): Symbol.from_row(row) for row in rows}
        visible_versions = {(row["file_path"], row["content_hash"]) for row in states}
        results = [
            symbol
            for hit in hits
            if (symbol := symbols_by_id.get(hit.id)) is not None
            and (symbol.file_path, symbol.file_content_hash) in visible_versions
        ]
        return results[:limit]

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
