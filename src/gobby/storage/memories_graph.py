from gobby.storage.memories_base import MemoryStoreBase
from gobby.storage.memories_models import Memory


class MemoryGraphMixin(MemoryStoreBase):
    def mark_pending_graph(self, memory_id: str) -> None:
        """Mark a memory as pending KG graph processing."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories SET graph_processed = FALSE WHERE id = %s",
                (memory_id,),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Memory not found: {memory_id}")

    def mark_pending_graphs(self, project_id: str | None = None) -> int:
        """Mark multiple memories as pending KG graph processing.

        When ``project_id`` is provided, only memories in that project are reset.
        When omitted, all memories are reset.
        """
        with self.db.transaction() as conn:
            if project_id is None:
                cursor = conn.execute("UPDATE memories SET graph_processed = FALSE")
            else:
                cursor = conn.execute(
                    "UPDATE memories SET graph_processed = FALSE WHERE project_id = %s",
                    (project_id,),
                )
            return cursor.rowcount

    def mark_graph_processed(self, memory_id: str) -> None:
        """Mark a memory as having been processed by the KG pipeline."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE memories SET graph_processed = TRUE WHERE id = %s",
                (memory_id,),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Memory not found: {memory_id}")

    def list_all_ids(self, *, limit: int | None = None, offset: int = 0) -> list[str]:
        """Return memory IDs from the database.

        Args:
            limit: Max number of IDs to return. None returns all.
            offset: Number of rows to skip before returning results.
        """
        if limit is not None:
            rows = self.db.fetchall("SELECT id FROM memories LIMIT %s OFFSET %s", (limit, offset))
        elif offset:
            rows = self.db.fetchall("SELECT id FROM memories OFFSET %s", (offset,))
        else:
            rows = self.db.fetchall("SELECT id FROM memories")
        return [row["id"] for row in rows]

    def get_pending_graph_memories(self, limit: int = 20) -> list[Memory]:
        """Get memories pending KG graph processing."""
        rows = self.db.fetchall(
            "SELECT * FROM memories WHERE graph_processed IS FALSE ORDER BY created_at ASC LIMIT %s",
            (limit,),
        )
        return [Memory.from_row(row) for row in rows]
