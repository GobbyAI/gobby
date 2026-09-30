"""Vector projection repair bookkeeping for stored memories."""

from gobby.storage.memories_base import MemoryStoreBase


class MemoryVectorReindexMixin(MemoryStoreBase):
    def list_vector_reindex_ids(self) -> list[str]:
        """Return memories whose stored content is newer than their vector."""
        rows = self.db.fetchall(
            "SELECT id FROM memories WHERE vector_needs_reindex IS TRUE "
            "AND deleted_at IS NULL ORDER BY id"
        )
        return [str(row["id"]) for row in rows]

    def mark_vector_reindex_needed(self, memory_id: str) -> None:
        """Mark one memory for a retryable vector projection repair."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories SET vector_needs_reindex = TRUE WHERE id = %s",
                (memory_id,),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Memory {memory_id} not found")

    def mark_vectors_reindexed(self, indexed_content: dict[str, str]) -> int:
        """Clear stale state only when the indexed content is still current."""
        if not indexed_content:
            return 0
        cleared = 0
        with self.db.transaction() as conn:
            for memory_id, content in indexed_content.items():
                cursor = conn.execute(
                    """
                    UPDATE memories
                    SET vector_needs_reindex = (content IS DISTINCT FROM %s)
                    WHERE id = %s
                    RETURNING content IS NOT DISTINCT FROM %s AS content_matched
                    """,
                    (content, memory_id, content),
                )
                row = cursor.fetchone()
                if row is not None and row["content_matched"]:
                    cleared += 1
        return cleared

    def mark_vector_snapshot_reindexed(
        self,
        memory_id: str,
        content: str,
        project_id: str,
        is_global: bool,
    ) -> bool:
        """Clear repair intent only when the full scheduling identity still matches."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE memories
                SET vector_needs_reindex = FALSE
                WHERE id = %s
                  AND content = %s
                  AND project_id = %s
                  AND is_global = %s
                  AND deleted_at IS NULL
                """,
                (memory_id, content, project_id, is_global),
            )
            updated = cursor.rowcount
        if updated:
            self.notify_changed()
        return bool(updated)

    def reconcile_vector_snapshot_page(
        self,
        snapshots: list[tuple[str, str, str, bool]],
        reindex_ids: list[str],
    ) -> set[str]:
        """CAS-clear one rebuild page and requeue changed identities atomically."""
        cleared_ids: set[str] = set()
        changed = False
        with self.db.transaction() as conn:
            if snapshots:
                cursor = conn.execute(
                    """
                    UPDATE memories AS memory
                    SET vector_needs_reindex = FALSE
                    FROM UNNEST(
                        %s::uuid[],
                        %s::text[],
                        %s::uuid[],
                        %s::boolean[]
                    ) AS snapshot(id, content, project_id, is_global)
                    WHERE memory.id = snapshot.id
                      AND memory.content = snapshot.content
                      AND memory.project_id = snapshot.project_id
                      AND memory.is_global = snapshot.is_global
                      AND memory.deleted_at IS NULL
                    RETURNING memory.id
                    """,
                    (
                        [row[0] for row in snapshots],
                        [row[1] for row in snapshots],
                        [row[2] for row in snapshots],
                        [row[3] for row in snapshots],
                    ),
                )
                cleared_ids = {str(row["id"]) for row in cursor.fetchall()}
                changed = bool(cleared_ids)

            failed_snapshot_ids = {row[0] for row in snapshots} - cleared_ids
            ids_to_reindex = sorted({*reindex_ids, *failed_snapshot_ids})
            if ids_to_reindex:
                cursor = conn.execute(
                    """
                    UPDATE memories
                    SET vector_needs_reindex = TRUE
                    WHERE id = ANY(%s::uuid[])
                    """,
                    (ids_to_reindex,),
                )
                changed = changed or bool(cursor.rowcount)
        if changed:
            self.notify_changed()
        return cleared_ids
