"""Hub-backed fallback helpers for graph-style UI views."""

from __future__ import annotations

from typing import Any

from gobby.storage.hub.protocol import HubDatabase


class CodeIndexGraphFallbackStorageMixin:
    """Storage methods that build graph-shaped responses from hub rows."""

    db: HubDatabase

    def get_file_symbol_tree(self, project_id: str, limit: int = 200) -> dict[str, Any]:
        """Build file-to-symbol containment graph from indexed hub rows."""
        file_rows = self.db.fetchall(
            """SELECT f.file_path, f.language, f.symbol_count
               FROM code_indexed_files f
               WHERE f.project_id = %s
               ORDER BY
                 CASE WHEN f.language IN ('markdown','yaml','json')
                      THEN 1 ELSE 0 END,
                 f.symbol_count DESC, f.file_path
               LIMIT %s""",
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

        if file_paths:
            placeholders = ",".join("%s" for _ in file_paths)
            sym_rows = self.db.fetchall(
                f"""SELECT id, name, kind, file_path, line_start, signature
                    FROM code_symbols
                    WHERE project_id = %s AND file_path IN ({placeholders})
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
