"""Symbol summary storage helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from gobby.code_index._storage.constants import SYNC_FAILURE_COOLOFF_SECONDS
from gobby.code_index.models import Symbol
from gobby.code_index.summary_safety import sanitize_symbol_summary
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.machine_id import require_machine_id


class CodeIndexSummaryStorageMixin:
    """Storage methods for symbol summary generation state."""

    db: HubDatabase

    def get_unsummarized_symbols(
        self,
        project_id: str,
        kinds: list[str] | None = None,
        limit: int = 20,
        failure_cooloff_seconds: int = SYNC_FAILURE_COOLOFF_SECONDS,
    ) -> list[Symbol]:
        """Get symbols that have no summary yet."""
        if kinds is None:
            kinds = ["function", "class", "method"]
        placeholders = ",".join("%s" for _ in kinds)
        retry_cutoff = (datetime.now(UTC) - timedelta(seconds=failure_cooloff_seconds)).isoformat()
        rows = self.db.fetchall(
            f"""SELECT s.* FROM code_symbols s
                JOIN code_indexed_file_states fs
                  ON fs.project_id = s.project_id
                 AND fs.file_path = s.file_path
                 AND fs.content_hash = s.file_content_hash
                WHERE fs.machine_id = %s AND fs.project_id = %s AND s.summary IS NULL
                  AND (s.summary_attempted_at IS NULL OR s.summary_attempted_at < %s)
                  AND s.kind IN ({placeholders})
                ORDER BY s.updated_at DESC
                LIMIT %s""",
            (require_machine_id(), project_id, retry_cutoff, *kinds, limit),
        )
        return [Symbol.from_row(r) for r in rows]

    def update_symbol_summary(self, symbol_id: str, content_hash: str, summary: str) -> bool:
        """Set the summary for a symbol if the content hash still matches."""
        sanitized_summary = sanitize_symbol_summary(summary)
        if sanitized_summary is None:
            return False

        with self.db.transaction() as conn:
            cursor = conn.execute(
                """UPDATE code_symbols
                   SET summary = %s, summary_attempted_at = NULL
                   WHERE id = %s AND content_hash = %s""",
                (sanitized_summary, symbol_id, content_hash),
            )
            return cursor.rowcount > 0

    def mark_symbol_summaries_attempted(self, symbols: list[tuple[str, str]]) -> int:
        """Mark summary generation attempts for symbols that failed to produce summaries."""
        if not symbols:
            return 0
        now = datetime.now(UTC).isoformat()
        placeholders = ",".join("(%s::uuid, %s)" for _ in symbols)
        params = [value for symbol in symbols for value in symbol]
        with self.db.transaction() as conn:
            cursor = conn.execute(
                f"""UPDATE code_symbols AS s
                    SET summary_attempted_at = %s
                    FROM (VALUES {placeholders}) AS v(id, content_hash)
                    WHERE s.id = v.id
                      AND s.content_hash = v.content_hash
                      AND s.summary IS NULL""",
                (now, *params),
            )
            return cursor.rowcount
