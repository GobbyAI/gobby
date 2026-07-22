from datetime import datetime
from typing import Any, Literal

from gobby.storage.memories_base import MemoryStoreBase
from gobby.storage.memories_models import Memory, validate_memory_type
from gobby.storage.memories_scope import MemoryScope, memory_scope_predicate
from gobby.storage.sql_dialect import (
    json_array_contains_condition,
    json_empty_array_coalesce_expr,
    older_than_now_expr,
)
from gobby.utils.datetime import parse_stored_datetime, utc_now


class MemoryDreamMixin(MemoryStoreBase):
    def mark_memories_due(
        self,
        memory_ids: list[str],
        *,
        expected_project_id: str | None,
    ) -> int:
        """Mark listed active memories due when their scope still matches."""
        if not memory_ids:
            return 0

        if expected_project_id is None:
            scope_sql = "is_global IS TRUE"
            params: tuple[Any, ...] = (memory_ids,)
        else:
            scope_sql = "project_id = %s AND is_global IS FALSE"
            params = (memory_ids, expected_project_id)

        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories SET last_dreamed_at = NULL, "
                "dream_due_version = dream_due_version + 1 "
                "WHERE id = ANY(%s::uuid[]) AND deleted_at IS NULL AND " + scope_sql,
                params,
            )
            affected = cursor.rowcount
        if affected:
            self.notify_changed()
        return affected

    def mark_dreamed_with_connection(
        self,
        conn: Any,
        memory_id: str,
        *,
        hidden_as: Literal["review", "delete"] | None = None,
        when: datetime | str | None = None,
    ) -> bool:
        """Apply dream visibility bookkeeping on an existing transaction."""
        stamp = parse_stored_datetime(when) or utc_now()
        if hidden_as is None:
            sql = "UPDATE memories SET last_dreamed_at = %s WHERE id = %s"
            params: tuple[Any, ...] = (stamp, memory_id)
        else:
            sql = (
                "UPDATE memories SET last_dreamed_at = %s, deleted_at = %s, "
                "dream_action = %s WHERE id = %s"
            )
            params = (stamp, stamp, hidden_as, memory_id)
        cursor = conn.execute(sql, params)
        if cursor.rowcount == 0:
            raise ValueError(f"Memory {memory_id} not found")
        return True

    def mark_dreamed(
        self,
        memory_id: str,
        *,
        hidden_as: Literal["review", "delete"] | None = None,
        when: datetime | str | None = None,
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
        with self.db.transaction() as conn:
            self.mark_dreamed_with_connection(
                conn,
                memory_id,
                hidden_as=hidden_as,
                when=when,
            )
        self.notify_changed()
        return True

    def mark_project_memories_due(self, project_id: str) -> int:
        """Clear ``last_dreamed_at`` for a project's live memories.

        The truth-change trigger calls this when a project's codewiki truth
        digest changes: clearing the cooldown cursor makes every live memory in
        the project "due" again, so the next sweep re-judges them against the
        new stack without waiting for the per-memory cooldown to elapse. Global
        rows are deliberately excluded; platform truth changes reset those via
        ``mark_global_memories_due``. Only already-stamped, non-deleted rows are
        touched (soft-hidden rows and never-dreamed rows are left as-is).
        Returns the number of rows reset.
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories SET last_dreamed_at = NULL, "
                "dream_due_version = dream_due_version + 1 "
                "WHERE project_id = %s AND is_global IS FALSE AND deleted_at IS NULL "
                "AND last_dreamed_at IS NOT NULL",
                (project_id,),
            )
            affected = cursor.rowcount
        if affected:
            self._notify_listeners()
        return affected

    def mark_global_memories_due(self) -> int:
        """Clear ``last_dreamed_at`` for global live memories only."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories SET last_dreamed_at = NULL, "
                "dream_due_version = dream_due_version + 1 "
                "WHERE is_global IS TRUE AND deleted_at IS NULL "
                "AND last_dreamed_at IS NOT NULL"
            )
            affected = cursor.rowcount
        if affected:
            self._notify_listeners()
        return affected

    def restore_memory(self, memory_id: str, when: datetime | str | None = None) -> bool:
        """Reactivate a soft-hidden memory.

        Clears ``deleted_at`` and ``dream_action`` and stamps ``last_dreamed_at``
        so a freshly restored memory is not immediately re-dreamed by the next
        sweep (the cooldown applies). ``updated_at`` is left untouched.

        Raises ``ValueError`` if the memory does not exist.
        """
        stamp = parse_stored_datetime(when) or utc_now()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT vector_needs_reindex FROM memories WHERE id = %s FOR UPDATE",
                (memory_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Memory {memory_id} not found")
            if bool(row["vector_needs_reindex"]):
                conn.execute(
                    "DELETE FROM memory_crossrefs WHERE source_id = %s OR target_id = %s",
                    (memory_id, memory_id),
                )
            cursor = conn.execute(
                "UPDATE memories SET deleted_at = NULL, dream_action = NULL, "
                "last_dreamed_at = %s, vector_needs_reindex = TRUE, "
                "graph_processed = FALSE, graph_status = 'pending' WHERE id = %s",
                (stamp, memory_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Memory {memory_id} not found")
        self.notify_changed()
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

    def _dream_candidate_filter(
        self,
        *,
        redream_cutoff: datetime | str,
        scope: MemoryScope,
        memory_type: str | None,
    ) -> tuple[str, list[Any]]:
        clauses = [
            "deleted_at IS NULL",
            "(last_dreamed_at IS NULL OR last_dreamed_at < %s)",
        ]
        cutoff = parse_stored_datetime(redream_cutoff)
        if cutoff is None:
            raise ValueError("redream_cutoff is required")
        params: list[Any] = [cutoff]
        review_lesson_condition, review_lesson_params = json_array_contains_condition(
            self.db,
            json_empty_array_coalesce_expr(self.db, "tags"),
            "review-lesson",
        )
        clauses.append(f"NOT ({review_lesson_condition})")
        params.extend(review_lesson_params)
        scope_predicate, scope_params = memory_scope_predicate(scope)
        if scope_predicate:
            clauses.append(scope_predicate)
            params.extend(scope_params)
        if memory_type is not None:
            clauses.append("memory_type = %s")
            params.append(validate_memory_type(memory_type).value)
        return " AND ".join(clauses), params

    def list_dream_candidates(
        self,
        *,
        limit: int,
        redream_cutoff: datetime | str,
        scope: MemoryScope,
        memory_type: str | None = None,
    ) -> list[Memory]:
        """Return the next page of active memories due for a dream sweep.

        Selects visible rows (``deleted_at IS NULL``) that have either never been
        dreamed or were last dreamed before ``redream_cutoff`` (the cooldown
        boundary, ``run_started_at - redream_after_hours``). Review-lesson
        memories are protected from dream mutations and excluded before paging.
        Ownership/visibility and memory-type scoping is applied in SQL. Ordered
        oldest-dreamed first so the sweep drains deterministically.
        """
        where, params = self._dream_candidate_filter(
            redream_cutoff=redream_cutoff,
            scope=scope,
            memory_type=memory_type,
        )
        params.append(limit)
        rows = self.db.fetchall(
            f"SELECT * FROM memories WHERE {where} "  # nosec B608
            "ORDER BY last_dreamed_at ASC NULLS FIRST, updated_at ASC, id ASC LIMIT %s",
            tuple(params),
        )
        return [Memory.from_row(row) for row in rows]

    def list_dream_candidate_ids(
        self,
        *,
        redream_cutoff: datetime | str,
        scope: MemoryScope,
        memory_type: str | None = None,
    ) -> list[str]:
        """Materialize the stable ordered IDs eligible at sweep start."""
        where, params = self._dream_candidate_filter(
            redream_cutoff=redream_cutoff,
            scope=scope,
            memory_type=memory_type,
        )
        rows = self.db.fetchall(
            f"SELECT id FROM memories WHERE {where} "  # nosec B608
            "ORDER BY last_dreamed_at ASC NULLS FIRST, updated_at ASC, id ASC",
            tuple(params),
        )
        return [str(row["id"]) for row in rows]

    def list_dream_scopes(self, *, redream_cutoff: datetime | str) -> list[MemoryScope]:
        """Return distinct explicit scopes that have due memory dream work."""
        cutoff = parse_stored_datetime(redream_cutoff)
        if cutoff is None:
            raise ValueError("redream_cutoff is required")
        rows = self.db.fetchall(
            "SELECT DISTINCT project_id, is_global FROM memories "
            "WHERE deleted_at IS NULL "
            "AND (last_dreamed_at IS NULL OR last_dreamed_at < %s) "
            "ORDER BY is_global ASC, project_id ASC",
            (cutoff,),
        )
        scopes = [
            MemoryScope.project_only(str(row["project_id"])) for row in rows if not row["is_global"]
        ]
        if any(row["is_global"] for row in rows):
            scopes.append(MemoryScope.global_only())
        return scopes
