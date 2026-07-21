from gobby.storage.memories_base import MemoryStoreBase
from gobby.storage.memories_models import MemoryCrossRef
from gobby.storage.memories_scope import ALL_MEMORIES, MemoryScope, memory_scope_predicate
from gobby.utils.datetime import utc_now


class MemoryCrossRefMixin(MemoryStoreBase):
    def create_crossref(
        self,
        source_id: str,
        target_id: str,
        similarity: float,
    ) -> MemoryCrossRef:
        """
        Create a cross-reference link between two memories.

        Args:
            source_id: The source memory ID
            target_id: The target memory ID
            similarity: Similarity score (0.0 to 1.0)

        Returns:
            The created MemoryCrossRef

        Note:
            If the crossref already exists, it will be updated with
            the new similarity score.
        """
        if not 0.0 <= similarity <= 1.0:
            raise ValueError("similarity must be between 0.0 and 1.0")

        now = utc_now()

        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO memory_crossrefs (source_id, target_id, similarity, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(source_id, target_id) DO UPDATE SET
                    similarity = excluded.similarity
                """,
                (source_id, target_id, similarity, now),
            )

        return MemoryCrossRef(
            source_id=source_id,
            target_id=target_id,
            similarity=similarity,
            created_at=now,
        )

    def get_crossrefs(
        self,
        memory_id: str,
        limit: int = 10,
        min_similarity: float = 0.0,
    ) -> list[MemoryCrossRef]:
        """
        Get cross-references for a memory (both as source and target).

        Args:
            memory_id: The memory ID to find links for
            limit: Maximum number of results
            min_similarity: Minimum similarity threshold

        Returns:
            List of MemoryCrossRef objects, sorted by similarity descending
        """
        # Get crossrefs where this memory is the source
        rows = self.db.fetchall(
            """
            SELECT source_id, target_id, similarity, created_at
            FROM memory_crossrefs
            WHERE source_id = %s AND similarity >= %s
            UNION
            SELECT source_id, target_id, similarity, created_at
            FROM memory_crossrefs
            WHERE target_id = %s AND similarity >= %s
            ORDER BY similarity DESC
            LIMIT %s
            """,
            (memory_id, min_similarity, memory_id, min_similarity, limit),
        )

        return [MemoryCrossRef.from_row(row) for row in rows]

    def delete_crossrefs(self, memory_id: str) -> int:
        """
        Delete all cross-references involving a memory.

        Called automatically when a memory is deleted due to CASCADE,
        but can be called manually for cleanup.

        Args:
            memory_id: The memory ID to delete crossrefs for

        Returns:
            Number of crossrefs deleted
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                DELETE FROM memory_crossrefs
                WHERE source_id = %s OR target_id = %s
                """,
                (memory_id, memory_id),
            )
            return cursor.rowcount

    def delete_project_crossrefs(self, project_id: str) -> int:
        """Delete all cross-references for memories belonging to a project.

        Args:
            project_id: The project ID whose crossrefs should be deleted.

        Returns:
            Number of crossrefs deleted.
        """
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                WITH project_memories AS (
                    SELECT id FROM memories WHERE project_id = %s
                )
                DELETE FROM memory_crossrefs
                WHERE source_id IN (SELECT id FROM project_memories)
                   OR target_id IN (SELECT id FROM project_memories)
                """,
                (project_id,),
            )
            return cursor.rowcount

    def get_all_crossrefs(
        self,
        scope: MemoryScope = ALL_MEMORIES,
        limit: int = 1000,
    ) -> list[MemoryCrossRef]:
        """
        Get all cross-references with visibility applied to both endpoints.

        Useful for building memory graphs.

        Args:
            scope: Explicit endpoint visibility scope
            limit: Maximum number of results

        Returns:
            List of MemoryCrossRef objects
        """
        source_predicate, source_params = memory_scope_predicate(scope, table_alias="m1")
        target_predicate, target_params = memory_scope_predicate(scope, table_alias="m2")
        where = (
            f"WHERE {source_predicate} AND {target_predicate}"
            if source_predicate and target_predicate
            else ""
        )
        rows = self.db.fetchall(
            f"""
            SELECT DISTINCT mc.source_id, mc.target_id, mc.similarity, mc.created_at
            FROM memory_crossrefs mc
            JOIN memories m1 ON mc.source_id = m1.id
            JOIN memories m2 ON mc.target_id = m2.id
            {where}
            ORDER BY mc.similarity DESC
            LIMIT %s
            """,  # nosec B608
            (*source_params, *target_params, limit),
        )

        return [MemoryCrossRef.from_row(row) for row in rows]
