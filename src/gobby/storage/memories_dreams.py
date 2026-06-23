from datetime import UTC, datetime
from typing import Any, Literal

from gobby.storage.memories_base import MemoryStoreBase
from gobby.storage.memories_models import Memory
from gobby.storage.sql_dialect import older_than_now_expr


class MemoryDreamMixin(MemoryStoreBase):
    def mark_dreamed(
        self,
        memory_id: str,
        *,
        hidden_as: Literal["review", "delete"] | None = None,
        when: str | None = None,
    ) -> bool:
        """Stamp ``last_dreamed_at`` and optionally soft-hide a dreamed memory.

        Direct SQL that deliberately never touches ``updated_at`` — dream review
        is GC bookkeeping, not a content edit, and bumping ``updated_at`` would
        distort recency and temporal decay. When ``hidden_as`` is ``None`` the
        row is only stamped (the ``keep`` case); when it is ``"review"`` or
        ``"delete"`` the row is soft-hidden (``deleted_at`` set, ``dream_action``
        recorded) so agent-facing reads skip it while it stays recoverable.

        Raises ``ValueError`` if the memory does not exist.
        """
        stamp = when or datetime.now(UTC).isoformat()
        if hidden_as is None:
            sql = "UPDATE memories SET last_dreamed_at = %s WHERE id = %s"
            params: tuple[Any, ...] = (stamp, memory_id)
        else:
            sql = (
                "UPDATE memories SET last_dreamed_at = %s, deleted_at = %s, "
                "dream_action = %s WHERE id = %s"
            )
            params = (stamp, stamp, hidden_as, memory_id)
        with self.db.transaction() as conn:
            cursor = conn.execute(sql, params)
            if cursor.rowcount == 0:
                raise ValueError(f"Memory {memory_id} not found")
        self._notify_listeners()
        return True

    def restore_memory(self, memory_id: str, when: str | None = None) -> bool:
        """Reactivate a soft-hidden memory.

        Clears ``deleted_at`` and ``dream_action`` and stamps ``last_dreamed_at``
        so a freshly restored memory is not immediately re-dreamed by the next
        sweep (the cooldown applies). ``updated_at`` is left untouched.

        Raises ``ValueError`` if the memory does not exist.
        """
        stamp = when or datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories SET deleted_at = NULL, dream_action = NULL, "
                "last_dreamed_at = %s WHERE id = %s",
                (stamp, memory_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Memory {memory_id} not found")
        self._notify_listeners()
        return True

    def purge_dream_hidden(self, action: str, older_than_days: int) -> list[str]:
        """Hard-delete soft-hidden rows of one ``dream_action`` past their grace.

        Returns the IDs removed so callers can reconcile secondary stores
        (Qdrant payloads, knowledge graph) after the physical rows are gone.
        """
        cutoff = older_than_now_expr(self.db, "deleted_at", "%s", "day")
        with self.db.transaction() as conn:
            rows = conn.execute(
                f"DELETE FROM memories WHERE dream_action = %s "  # nosec B608
                f"AND deleted_at IS NOT NULL AND {cutoff} RETURNING id",
                (action, older_than_days),
            ).fetchall()
        ids = [row["id"] for row in rows]
        if ids:
            self._notify_listeners()
        return ids

    def list_dream_candidates(
        self,
        *,
        limit: int,
        redream_cutoff: str,
        project_id: str | None = None,
        memory_type: str | None = None,
        include_global: bool = True,
        global_only: bool = False,
    ) -> list[Memory]:
        """Return the next page of active memories due for a dream sweep.

        Selects visible rows (``deleted_at IS NULL``) that have either never been
        dreamed or were last dreamed before ``redream_cutoff`` (the cooldown
        boundary, ``run_started_at - redream_after_hours``). Project/global and
        memory-type scoping is applied in SQL, mirroring ``_in_scope``: a
        global-only run matches only NULL-scoped rows; otherwise, a ``project_id``
        with ``include_global`` also matches global rows; without it only that
        project's rows match; a ``None`` ``project_id`` sweeps every row. Ordered
        oldest-dreamed first so the sweep drains deterministically as each
        returned page is stamped out of the next page's window.
        """
        clauses = [
            "deleted_at IS NULL",
            "(last_dreamed_at IS NULL OR last_dreamed_at < %s)",
        ]
        params: list[Any] = [redream_cutoff]
        if global_only:
            clauses.append("project_id IS NULL")
        elif project_id is not None:
            if include_global:
                clauses.append("(project_id = %s OR project_id IS NULL)")
            else:
                clauses.append("project_id = %s")
            params.append(project_id)
        if memory_type is not None:
            clauses.append("memory_type = %s")
            params.append(memory_type)
        where = " AND ".join(clauses)
        params.append(limit)
        rows = self.db.fetchall(
            f"SELECT * FROM memories WHERE {where} "  # nosec B608
            "ORDER BY last_dreamed_at ASC NULLS FIRST, updated_at ASC LIMIT %s",
            tuple(params),
        )
        return [Memory.from_row(row) for row in rows]

    def list_dream_project_ids(self, *, redream_cutoff: str) -> list[str | None]:
        """Return distinct project scopes that have due memory dream work."""
        rows = self.db.fetchall(
            "SELECT DISTINCT project_id FROM memories "
            "WHERE deleted_at IS NULL "
            "AND (last_dreamed_at IS NULL OR last_dreamed_at < %s) "
            "ORDER BY project_id ASC NULLS LAST",
            (redream_cutoff,),
        )
        return [row["project_id"] for row in rows]
