from datetime import datetime
from typing import Any

from gobby.storage.memories_base import MemoryStoreBase
from gobby.storage.memories_models import Memory, Visibility, visibility_predicate
from gobby.storage.sql_dialect import json_array_contains_condition, json_empty_array_coalesce_expr
from gobby.utils.datetime import parse_stored_datetime


class MemoryQueryMixin(MemoryStoreBase):
    def count_memories(
        self,
        project_id: str | None = None,
        memory_type: str | None = None,
        *,
        visibility: Visibility = "active",
    ) -> int:
        """Return the total number of memories using COUNT(*).

        When project_id is provided, includes both project-specific memories
        and global memories (project_id IS NULL) since global memories are
        accessible from any project context.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if project_id:
            clauses.append("(project_id = %s OR project_id IS NULL)")
            params.append(project_id)
        if memory_type:
            clauses.append("memory_type = %s")
            params.append(memory_type)
        vis = visibility_predicate(visibility)
        if vis:
            clauses.append(vis)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        row = self.db.fetchone(
            f"SELECT COUNT(*) AS cnt FROM memories{where}",
            tuple(params),  # nosec B608
        )
        return row["cnt"] if row else 0

    def list_memories(
        self,
        project_id: str | None = None,
        memory_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
        *,
        visibility: Visibility = "active",
        include_global: bool = True,
    ) -> list[Memory]:
        """
        List memories with optional filtering.

        Args:
            project_id: Filter by project ID (or None for global)
            memory_type: Filter by memory type
            limit: Maximum number of results
            offset: Number of results to skip
            tags_all: Memory must have ALL of these tags
            tags_any: Memory must have at least ONE of these tags
            tags_none: Memory must have NONE of these tags

        Returns:
            List of matching memories
        """
        if limit <= 0:
            return []

        query = "SELECT * FROM memories WHERE 1=1"
        params: list[Any] = []

        if project_id:
            query += (
                " AND (project_id = %s OR project_id IS NULL)"
                if include_global
                else " AND project_id = %s"
            )
            params.append(project_id)

        if memory_type:
            query += " AND memory_type = %s"
            params.append(memory_type)

        vis = visibility_predicate(visibility)
        if vis:
            query += f" AND {vis}"

        tag_clause, tag_params = self._tag_filter_clause(tags_all, tags_any, tags_none)
        query += tag_clause
        params.extend(tag_params)

        query += " ORDER BY updated_at DESC"

        rows = self.db.fetchall(
            f"{query} LIMIT %s OFFSET %s",
            (*params, limit, offset),  # nosec B608
        )
        return [Memory.from_row(row) for row in rows]

    def update_access_stats(self, memory_id: str, accessed_at: datetime | str) -> None:
        """
        Update access count and last accessed timestamp for a memory.

        Args:
            memory_id: Memory ID to update
            accessed_at: Timestamp of access
        """
        accessed_at_dt = parse_stored_datetime(accessed_at)
        if accessed_at_dt is None:
            raise ValueError("accessed_at is required")
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE memories
                SET access_count = access_count + 1,
                    last_accessed_at = %s
                WHERE id = %s
                """,
                (accessed_at_dt, memory_id),
            )
            if cursor.rowcount == 0:
                raise ValueError(f"Memory not found: {memory_id}")

    def search_memories(
        self,
        query_text: str,
        project_id: str | None = None,
        limit: int = 20,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
        *,
        visibility: Visibility = "active",
        include_global: bool = True,
    ) -> list[Memory]:
        """
        Search memories by content with optional tag filtering.

        Args:
            query_text: Text to search for in memory content
            project_id: Optional project ID to filter by
            limit: Maximum number of results
            tags_all: Memory must have ALL of these tags
            tags_any: Memory must have at least ONE of these tags
            tags_none: Memory must have NONE of these tags

        Returns:
            List of matching memories
        """
        # Escape LIKE wildcards in query_text
        escaped_query = query_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = "SELECT * FROM memories WHERE content LIKE %s ESCAPE '\\'"
        params: list[Any] = [f"%{escaped_query}%"]

        if project_id:
            sql += (
                " AND (project_id = %s OR project_id IS NULL)"
                if include_global
                else " AND project_id = %s"
            )
            params.append(project_id)

        vis = visibility_predicate(visibility)
        if vis:
            sql += f" AND {vis}"

        tag_clause, tag_params = self._tag_filter_clause(tags_all, tags_any, tags_none)
        sql += tag_clause
        params.extend(tag_params)

        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(limit)

        rows = self.db.fetchall(sql, tuple(params))
        return [Memory.from_row(row) for row in rows]

    def _tag_filter_clause(
        self,
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
    ) -> tuple[str, list[Any]]:
        tag_column = json_empty_array_coalesce_expr(self.db, "tags")
        clauses: list[str] = []
        params: list[Any] = []

        for tag in tags_all or []:
            condition, condition_params = json_array_contains_condition(self.db, tag_column, tag)
            clauses.append(condition)
            params.extend(condition_params)

        if tags_any:
            any_clauses: list[str] = []
            for tag in tags_any:
                condition, condition_params = json_array_contains_condition(
                    self.db,
                    tag_column,
                    tag,
                )
                any_clauses.append(condition)
                params.extend(condition_params)
            clauses.append(f"({' OR '.join(any_clauses)})")

        for tag in tags_none or []:
            condition, condition_params = json_array_contains_condition(self.db, tag_column, tag)
            clauses.append(f"NOT ({condition})")
            params.extend(condition_params)

        if not clauses:
            return "", []
        return f" AND {' AND '.join(clauses)}", params

    def _filter_by_tags(
        self,
        memories: list[Memory],
        tags_all: list[str] | None = None,
        tags_any: list[str] | None = None,
        tags_none: list[str] | None = None,
    ) -> list[Memory]:
        """
        Filter memories by tag criteria.

        Args:
            memories: List of memories to filter
            tags_all: Memory must have ALL of these tags
            tags_any: Memory must have at least ONE of these tags
            tags_none: Memory must have NONE of the specified tags

        Returns:
            Filtered list of memories
        """
        result: list[Memory] = []
        for memory in memories:
            memory_tags = set(memory.tags) if memory.tags else set()

            # Check tags_all: memory must have ALL specified tags
            if tags_all:
                if not set(tags_all).issubset(memory_tags):
                    continue

            # Check tags_any: memory must have at least ONE specified tag
            if tags_any:
                if not memory_tags.intersection(tags_any):
                    continue

            # Check tags_none: memory must have NONE of the specified tags
            if tags_none:
                if memory_tags.intersection(tags_none):
                    continue

            result.append(memory)

        return result
