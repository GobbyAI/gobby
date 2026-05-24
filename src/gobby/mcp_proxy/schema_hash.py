"""Schema hash management for incremental tool re-indexing.

Tracks tool schema hashes to detect changes and enable incremental
updates rather than full re-indexing.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gobby.storage.hub.protocol import HubDatabase

logger = logging.getLogger(__name__)


def compute_schema_hash(input_schema: dict[str, Any] | None) -> str:
    """
    Compute a deterministic hash of a tool's input schema.

    Uses canonical JSON serialization to ensure consistent hashing
    regardless of key ordering.

    Args:
        input_schema: Tool's inputSchema as a dictionary

    Returns:
        16-character hex hash of the schema
    """
    if input_schema is None:
        return hashlib.sha256(b"null").hexdigest()[:16]

    # Use canonical JSON for deterministic serialization
    canonical = json.dumps(input_schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass
class SchemaHashRecord:
    """A stored schema hash record."""

    id: int
    server_name: str
    tool_name: str
    project_id: str
    schema_hash: str
    last_verified_at: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> "SchemaHashRecord":
        """Create from database row."""
        return cls(
            id=row["id"],
            server_name=row["server_name"],
            tool_name=row["tool_name"],
            project_id=row["project_id"],
            schema_hash=row["schema_hash"],
            last_verified_at=row["last_verified_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "project_id": self.project_id,
            "schema_hash": self.schema_hash,
            "last_verified_at": self.last_verified_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SchemaHashManager:
    """
    Manages tool schema hashes for incremental re-indexing.

    Tracks schema hashes to detect when tool definitions change,
    enabling efficient incremental updates instead of full re-indexing.
    """

    def __init__(self, db: HubDatabase):
        """
        Initialize the schema hash manager.

        Args:
            db: Hub database adapter for persistence
        """
        self.db = db

    def store_hash(
        self,
        server_name: str,
        tool_name: str,
        project_id: str,
        schema_hash: str,
    ) -> SchemaHashRecord:
        """
        Store or update a schema hash.

        Uses UPSERT to handle both new and existing records.

        Args:
            server_name: Name of the MCP server
            tool_name: Name of the tool
            project_id: Project ID
            schema_hash: Computed schema hash

        Returns:
            The stored SchemaHashRecord
        """
        now = datetime.now(UTC).isoformat()

        self.db.execute(
            """
            INSERT INTO tool_schema_hashes (
                server_name, tool_name, project_id, schema_hash,
                last_verified_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, server_name, tool_name) DO UPDATE SET
                schema_hash = excluded.schema_hash,
                last_verified_at = excluded.last_verified_at,
                updated_at = excluded.updated_at
            """,
            (server_name, tool_name, project_id, schema_hash, now, now, now),
        )

        result = self.get_hash(server_name, tool_name, project_id)
        if result is None:
            raise RuntimeError(f"Failed to retrieve hash for {server_name}/{tool_name} after store")
        return result

    def get_hash(
        self, server_name: str, tool_name: str, project_id: str
    ) -> SchemaHashRecord | None:
        """
        Get stored hash for a tool.

        Args:
            server_name: Server name
            tool_name: Tool name
            project_id: Project ID

        Returns:
            SchemaHashRecord or None if not found
        """
        row = self.db.fetchone(
            """
            SELECT * FROM tool_schema_hashes
            WHERE project_id = ? AND server_name = ? AND tool_name = ?
            """,
            (project_id, server_name, tool_name),
        )
        return SchemaHashRecord.from_row(row) if row else None

    def get_hashes_for_server(self, server_name: str, project_id: str) -> list[SchemaHashRecord]:
        """
        Get all hashes for a server.

        Args:
            server_name: Server name
            project_id: Project ID

        Returns:
            List of SchemaHashRecord
        """
        rows = self.db.fetchall(
            """
            SELECT * FROM tool_schema_hashes
            WHERE project_id = ? AND server_name = ?
            """,
            (project_id, server_name),
        )
        return [SchemaHashRecord.from_row(row) for row in rows]

    def needs_reindexing(
        self,
        server_name: str,
        tool_name: str,
        project_id: str,
        current_schema: dict[str, Any] | None,
    ) -> bool:
        """
        Check if a tool needs re-indexing based on schema hash.

        Args:
            server_name: Server name
            tool_name: Tool name
            project_id: Project ID
            current_schema: Current tool inputSchema

        Returns:
            True if schema is missing or changed
        """
        stored = self.get_hash(server_name, tool_name, project_id)
        if not stored:
            return True

        current_hash = compute_schema_hash(current_schema)
        return stored.schema_hash != current_hash

    def check_tools_for_changes(
        self,
        server_name: str,
        project_id: str,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """
        Check multiple tools for schema changes.

        Args:
            server_name: Server name
            project_id: Project ID
            tools: List of tool dicts with 'name' and 'inputSchema' keys

        Returns:
            Dict with 'changed', 'unchanged', 'new' tool lists
        """
        result: dict[str, list[str]] = {
            "changed": [],
            "unchanged": [],
            "new": [],
        }

        # Get all stored hashes for this server
        stored_hashes = {
            h.tool_name: h.schema_hash for h in self.get_hashes_for_server(server_name, project_id)
        }

        for tool in tools:
            tool_name = tool.get("name", "")
            schema = tool.get("inputSchema") or tool.get("input_schema")
            current_hash = compute_schema_hash(schema)

            if tool_name not in stored_hashes:
                result["new"].append(tool_name)
            elif stored_hashes[tool_name] != current_hash:
                result["changed"].append(tool_name)
            else:
                result["unchanged"].append(tool_name)

        return result

    def update_verification_time(self, server_name: str, tool_name: str, project_id: str) -> bool:
        """
        Update last_verified_at timestamp without changing hash.

        Useful for marking a hash as still valid after verification.

        Args:
            server_name: Server name
            tool_name: Tool name
            project_id: Project ID

        Returns:
            True if updated, False if not found
        """
        now = datetime.now(UTC).isoformat()
        cursor = self.db.execute(
            """
            UPDATE tool_schema_hashes
            SET last_verified_at = ?, updated_at = ?
            WHERE project_id = ? AND server_name = ? AND tool_name = ?
            """,
            (now, now, project_id, server_name, tool_name),
        )
        return cursor.rowcount > 0

    def delete_hash(self, server_name: str, tool_name: str, project_id: str) -> bool:
        """
        Delete a schema hash.

        Args:
            server_name: Server name
            tool_name: Tool name
            project_id: Project ID

        Returns:
            True if deleted, False if not found
        """
        cursor = self.db.execute(
            """
            DELETE FROM tool_schema_hashes
            WHERE project_id = ? AND server_name = ? AND tool_name = ?
            """,
            (project_id, server_name, tool_name),
        )
        return cursor.rowcount > 0

    def delete_hashes_for_server(self, server_name: str, project_id: str) -> int:
        """
        Delete all hashes for a server.

        Args:
            server_name: Server name
            project_id: Project ID

        Returns:
            Number of rows deleted
        """
        cursor = self.db.execute(
            """
            DELETE FROM tool_schema_hashes
            WHERE project_id = ? AND server_name = ?
            """,
            (project_id, server_name),
        )
        return cursor.rowcount

    def cleanup_stale_hashes(
        self, server_name: str, project_id: str, valid_tool_names: list[str]
    ) -> int:
        """
        Remove hashes for tools that no longer exist on server.

        Args:
            server_name: Server name
            project_id: Project ID
            valid_tool_names: List of tool names that still exist

        Returns:
            Number of stale hashes deleted
        """
        if not valid_tool_names:
            return self.delete_hashes_for_server(server_name, project_id)

        # Build placeholders for IN clause
        placeholders = ",".join("?" for _ in valid_tool_names)
        cursor = self.db.execute(
            f"DELETE FROM tool_schema_hashes WHERE project_id = ? AND server_name = ? AND tool_name NOT IN ({placeholders})",  # nosec B608
            (project_id, server_name, *valid_tool_names),
        )
        return cursor.rowcount

    def get_stats(self, project_id: str | None = None) -> dict[str, Any]:
        """
        Get statistics about stored schema hashes.

        Args:
            project_id: Optional project filter

        Returns:
            Dict with count, by_server breakdown
        """
        if project_id:
            count_row = self.db.fetchone(
                "SELECT COUNT(*) as count FROM tool_schema_hashes WHERE project_id = ?",
                (project_id,),
            )
            server_rows = self.db.fetchall(
                """
                SELECT server_name, COUNT(*) as count
                FROM tool_schema_hashes
                WHERE project_id = ?
                GROUP BY server_name
                """,
                (project_id,),
            )
        else:
            count_row = self.db.fetchone("SELECT COUNT(*) as count FROM tool_schema_hashes")
            server_rows = self.db.fetchall(
                """
                SELECT server_name, COUNT(*) as count
                FROM tool_schema_hashes
                GROUP BY server_name
                """
            )

        return {
            "total_hashes": count_row["count"] if count_row else 0,
            "by_server": {row["server_name"]: row["count"] for row in server_rows},
        }
