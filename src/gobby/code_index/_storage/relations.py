"""Import and call relation storage helpers."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePath, PureWindowsPath
from typing import Any

from gobby.code_index.models import CallRelation, ImportRelation
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.machine_id import require_machine_id


def _validate_relation_path(file_path: str) -> None:
    if not file_path:
        raise ValueError("relation path must not be empty")
    if PurePath(file_path).is_absolute() or PureWindowsPath(file_path).is_absolute():
        raise ValueError("relation path must be repository-relative")
    if ".." in PurePath(file_path).parts or ".." in PureWindowsPath(file_path).parts:
        raise ValueError("relation path must not contain '..' segments")


class CodeIndexRelationStorageMixin:
    """Storage methods for import and call relations."""

    db: HubDatabase

    def _current_file_content_hash(self, project_id: str, file_path: str) -> str:
        row = self.db.fetchone(
            """SELECT content_hash FROM code_indexed_file_states
               WHERE machine_id = %s AND project_id = %s AND file_path = %s""",
            (require_machine_id(), project_id, file_path),
        )
        if row is None:
            raise ValueError(f"File {file_path!r} has no local code-index state")
        return str(row["content_hash"])

    def upsert_imports(
        self,
        project_id: str,
        file_path: str,
        imports: list[ImportRelation],
    ) -> int:
        """Idempotently store imports for the current immutable content version."""
        if not imports:
            return 0
        _validate_relation_path(file_path)
        for relation in imports:
            _validate_relation_path(relation.source_file)
        content_hash = self._current_file_content_hash(project_id, file_path)
        with self.db.transaction() as conn:
            cursor = conn.executemany(
                """INSERT INTO code_imports
                   (project_id, source_file, content_hash, target_module)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (
                       project_id, source_file, content_hash, target_module
                   ) DO NOTHING""",
                [(project_id, imp.source_file, content_hash, imp.target_module) for imp in imports],
            )
            return cursor.rowcount

    def upsert_calls(
        self,
        project_id: str,
        file_path: str,
        calls: list[CallRelation],
    ) -> int:
        """Idempotently store calls for the current immutable content version."""
        if not calls:
            return 0
        _validate_relation_path(file_path)
        for relation in calls:
            _validate_relation_path(relation.file_path)
        content_hash = self._current_file_content_hash(project_id, file_path)
        with self.db.transaction() as conn:
            cursor = conn.executemany(
                """INSERT INTO code_calls
                   (
                       project_id, caller_symbol_id, callee_symbol_id, callee_name,
                       callee_target_kind, callee_external_module, file_path,
                       content_hash, line
                   )
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (
                       project_id, file_path, content_hash, caller_symbol_id,
                       callee_symbol_id, callee_name, callee_target_kind,
                       callee_external_module, line
                   ) DO NOTHING""",
                [
                    (
                        project_id,
                        c.caller_symbol_id,
                        # callee_symbol_id is a nullable uuid column; external
                        # calls carry NULL (uniqueness is NULLS NOT DISTINCT).
                        c.callee_symbol_id,
                        c.callee_name,
                        c.callee_target_kind,
                        c.callee_external_module or "",
                        c.file_path,
                        content_hash,
                        c.line,
                    )
                    for c in calls
                ],
            )
            return cursor.rowcount

    def get_imports_for_file(self, project_id: str, file_path: str) -> list[dict[str, Any]]:
        """Get import relations for a file (for graph sync)."""
        rows = self.db.fetchall(
            """SELECT i.source_file, i.target_module
               FROM code_indexed_file_states fs
               JOIN code_imports i
                 ON i.project_id = fs.project_id
                AND i.source_file = fs.file_path
                AND i.content_hash = fs.content_hash
               WHERE fs.machine_id = %s AND fs.project_id = %s AND fs.file_path = %s""",
            (require_machine_id(), project_id, file_path),
        )
        return [
            {"source_file": r["source_file"], "target_module": r["target_module"]} for r in rows
        ]

    def find_files_importing_modules(
        self,
        project_id: str,
        module_candidates: Sequence[str],
    ) -> list[dict[str, Any]]:
        """Return source files with import edges to any canonical module candidate."""
        if not module_candidates:
            return []
        rows = self.db.fetchall(
            """SELECT DISTINCT i.source_file
               FROM code_indexed_file_states fs
               JOIN code_imports i
                 ON i.project_id = fs.project_id
                AND i.source_file = fs.file_path
                AND i.content_hash = fs.content_hash
               WHERE fs.machine_id = %s AND fs.project_id = %s
                 AND i.target_module = ANY(%s)
               ORDER BY i.source_file""",
            (require_machine_id(), project_id, list(module_candidates)),
        )
        return [{"file_path": row["source_file"]} for row in rows]

    def get_calls_for_file(self, project_id: str, file_path: str) -> list[dict[str, Any]]:
        """Get call relations for a file (for graph sync)."""
        rows = self.db.fetchall(
            """SELECT c.caller_symbol_id, c.callee_symbol_id, c.callee_name,
                      c.callee_target_kind, c.callee_external_module, c.file_path, c.line
               FROM code_indexed_file_states fs
               JOIN code_calls c
                 ON c.project_id = fs.project_id
                AND c.file_path = fs.file_path
                AND c.content_hash = fs.content_hash
               WHERE fs.machine_id = %s AND fs.project_id = %s AND fs.file_path = %s""",
            (require_machine_id(), project_id, file_path),
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
