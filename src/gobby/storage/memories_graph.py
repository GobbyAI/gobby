from gobby.storage.memories_base import MemoryStoreBase
from gobby.storage.memories_models import Memory
from gobby.storage.memories_scope import ALL_MEMORIES, MemoryScope, memory_scope_predicate


class MemoryGraphMixin(MemoryStoreBase):
    def mark_pending_graph(self, memory_id: str) -> None:
        """Mark a memory as pending KG graph processing."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE memories
                SET graph_processed = FALSE, graph_attempts = 0, graph_status = 'pending'
                WHERE id = %s
                """,
                (memory_id,),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Memory not found: {memory_id}")

    def mark_pending_graphs(self, scope: MemoryScope = ALL_MEMORIES) -> int:
        """Mark memories in an explicit scope as pending graph processing."""
        predicate, params = memory_scope_predicate(scope)
        where = f" WHERE {predicate}" if predicate else ""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories "
                "SET graph_processed = FALSE, graph_attempts = 0, graph_status = 'pending'"
                f"{where}",  # nosec B608
                params,
            )
            return cursor.rowcount

    def mark_graph_processed(self, memory_id: str) -> None:
        """Mark a memory as having been processed by the KG pipeline."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE memories
                SET graph_processed = TRUE, graph_attempts = 0, graph_status = 'completed'
                WHERE id = %s
                """,
                (memory_id,),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Memory not found: {memory_id}")

    def record_graph_failure(
        self,
        memory_id: str,
        *,
        deterministic: bool,
        max_attempts: int,
    ) -> str:
        """Persist a graph failure and return the resulting queue status.

        Retryable failures remain pending without consuming deterministic
        attempts. Deterministic failures become terminal at ``max_attempts``.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        with self.db.transaction() as conn:
            if deterministic:
                cursor = conn.execute(
                    """
                    UPDATE memories
                    SET graph_attempts = graph_attempts + 1,
                        graph_status = CASE
                            WHEN graph_attempts + 1 >= %s THEN 'failed'
                            ELSE 'pending'
                        END,
                        graph_processed = CASE
                            WHEN graph_attempts + 1 >= %s THEN TRUE
                            ELSE FALSE
                        END
                    WHERE id = %s
                    RETURNING graph_status
                    """,
                    (max_attempts, max_attempts, memory_id),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE memories
                    SET graph_processed = FALSE, graph_status = 'pending'
                    WHERE id = %s
                    RETURNING graph_status
                    """,
                    (memory_id,),
                )
            row = cursor.fetchone()
            if row is None:
                raise ValueError(f"Memory not found: {memory_id}")
            return str(row["graph_status"])

    def list_all_ids(self, *, limit: int | None = None, offset: int = 0) -> list[str]:
        """Return memory IDs from the database.

        Args:
            limit: Max number of IDs to return. None returns all.
            offset: Number of rows to skip before returning results.
        """
        if limit is not None:
            rows = self.db.fetchall(
                "SELECT id FROM memories ORDER BY id LIMIT %s OFFSET %s",
                (limit, offset),
            )
        elif offset:
            rows = self.db.fetchall("SELECT id FROM memories ORDER BY id OFFSET %s", (offset,))
        else:
            rows = self.db.fetchall("SELECT id FROM memories ORDER BY id")
        return [row["id"] for row in rows]

    def get_pending_graph_memories(self, limit: int = 20) -> list[Memory]:
        """Get memories pending KG graph processing."""
        rows = self.db.fetchall(
            """
            SELECT * FROM memories
            WHERE graph_processed IS FALSE
              AND graph_status = 'pending'
              AND deleted_at IS NULL
            ORDER BY created_at ASC
            LIMIT %s
            """,
            (limit,),
        )
        return [Memory.from_row(row) for row in rows]
