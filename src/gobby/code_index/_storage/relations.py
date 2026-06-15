"""Import and call relation storage helpers."""

from __future__ import annotations

from typing import Any

from gobby.code_index.models import CallRelation, ImportRelation
from gobby.storage.hub.protocol import HubDatabase


class CodeIndexRelationStorageMixin:
    """Storage methods for import and call relations."""

    db: HubDatabase

    def upsert_imports(
        self,
        project_id: str,
        file_path: str,
        imports: list[ImportRelation],
    ) -> int:
        """Replace import relations for a file. Returns count inserted."""
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM code_imports WHERE project_id = %s AND source_file = %s",
                (project_id, file_path),
            )
            if not imports:
                return 0
            conn.executemany(
                """INSERT INTO code_imports
                   (project_id, source_file, target_module)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (project_id, source_file, target_module) DO NOTHING""",
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
                "DELETE FROM code_calls WHERE project_id = %s AND file_path = %s",
                (project_id, file_path),
            )
            if not calls:
                return 0
            conn.executemany(
                """INSERT INTO code_calls
                   (
                       project_id, caller_symbol_id, callee_symbol_id, callee_name,
                       callee_target_kind, callee_external_module, file_path, line
                   )
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (
                       project_id, caller_symbol_id, callee_symbol_id, callee_name,
                       callee_target_kind, callee_external_module, file_path, line
                   ) DO NOTHING""",
                [
                    (
                        project_id,
                        c.caller_symbol_id,
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
               WHERE project_id = %s AND source_file = %s""",
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
               WHERE project_id = %s AND file_path = %s""",
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
                "DELETE FROM code_imports WHERE project_id = %s AND source_file = %s",
                (project_id, file_path),
            )
            return cursor.rowcount

    def delete_calls_for_file(self, project_id: str, file_path: str) -> int:
        """Delete call relations for a file. Returns count deleted."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM code_calls WHERE project_id = %s AND file_path = %s",
                (project_id, file_path),
            )
            return cursor.rowcount
