"""Project-scoped session reference renumbering."""

from __future__ import annotations

from typing import Protocol, TypedDict

from gobby.storage.hub.protocol import HubDatabase, SessionSeqMutation


class SessionRenumberMapping(TypedDict):
    """Old/new session reference mapping for a project compaction run."""

    session_id: str
    old_seq_num: int | None
    new_seq_num: int
    status: str
    title: str | None


class _RenumberHost(Protocol):
    db: HubDatabase


class _RenumberMixin:
    """Bulk maintenance helpers for session refs."""

    def renumber_project_sessions(
        self: _RenumberHost,
        project_id: str,
        *,
        dry_run: bool = True,
    ) -> list[SessionRenumberMapping]:
        """Assign dense per-project session refs.

        Non-deleted sessions keep the visible prefix of the ref range, ordered
        by creation time and id. Retained deleted rows are moved to the tail so
        old refs remain resolvable without punching holes in normal list views.
        """
        if not project_id:
            raise ValueError("project_id is required")

        with self.db.transaction_immediate(SessionSeqMutation(project_id=project_id)) as conn:
            rows = conn.execute(
                """
                SELECT id, seq_num, status, title
                FROM sessions
                WHERE project_id = %s
                ORDER BY
                    CASE WHEN status = 'deleted' THEN 1 ELSE 0 END,
                    created_at,
                    id
                """,
                (project_id,),
            ).fetchall()

            mapping: list[SessionRenumberMapping] = []
            for index, row in enumerate(rows, start=1):
                old_seq_num = row["seq_num"]
                mapping.append(
                    {
                        "session_id": str(row["id"]),
                        "old_seq_num": int(old_seq_num) if old_seq_num is not None else None,
                        "new_seq_num": index,
                        "status": str(row["status"]),
                        "title": None if row["title"] is None else str(row["title"]),
                    }
                )

            if dry_run or not mapping:
                return mapping

            old_seq_nums = [
                item["old_seq_num"] for item in mapping if item["old_seq_num"] is not None
            ]
            final_updates = [(item["new_seq_num"], item["session_id"]) for item in mapping]

            if old_seq_nums:
                min_seq_num = min(old_seq_nums)
                temp_offset = abs(min_seq_num) + len(mapping) + 1

                # Move rows through a disjoint negative range before writing final refs.
                # PostgreSQL checks per-project seq_num uniqueness immediately, so
                # mapping + old_seq_nums/min_seq_num/temp_offset reserve negative
                # values for the first executemany; the second applies final refs.
                conn.executemany(
                    "UPDATE sessions SET seq_num = %s WHERE id = %s",
                    [
                        (-(temp_offset + item["new_seq_num"]), item["session_id"])
                        for item in mapping
                    ],
                )
            conn.executemany("UPDATE sessions SET seq_num = %s WHERE id = %s", final_updates)

        return mapping
