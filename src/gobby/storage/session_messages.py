"""
Local storage for session messages.
"""

import asyncio
import json
import logging
from typing import Any

from gobby.sessions.transcripts.base import ParsedMessage
from gobby.storage.database import DatabaseProtocol

logger = logging.getLogger(__name__)


class LocalSessionMessageManager:
    """Manages storage of session messages and processing state."""

    def __init__(self, database: DatabaseProtocol):
        self.db = database

    async def store_messages(self, session_id: str, messages: list[ParsedMessage]) -> int:
        """
        Store parsed messages for a session.

        Args:
            session_id: ID of the session
            messages: List of ParsedMessage objects

        Returns:
            Number of messages stored
        """
        if not messages:
            return 0

        def _store_blocking() -> int:
            # Check if session exists to avoid FOREIGN KEY constraint errors
            # This can happen when sessions are created in hub-only mode but
            # message processor is using the project database
            session_exists = self.db.fetchone("SELECT 1 FROM sessions WHERE id = ?", (session_id,))
            if not session_exists:
                logger.debug(
                    f"Session {session_id} not found in database, skipping message storage"
                )
                return 0

            count = 0
            for msg in messages:
                # Convert dicts to JSON strings for storage
                tool_input = json.dumps(msg.tool_input) if msg.tool_input is not None else None
                tool_result = json.dumps(msg.tool_result) if msg.tool_result is not None else None
                raw_json = json.dumps(msg.raw_json) if msg.raw_json is not None else None

                query = """
                INSERT INTO session_messages (
                    session_id, message_index, role, content, content_type,
                    tool_name, tool_input, tool_result, tool_use_id, timestamp, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, message_index) DO UPDATE SET
                    content=excluded.content,
                    content_type=excluded.content_type,
                    tool_name=excluded.tool_name,
                    tool_input=excluded.tool_input,
                    tool_result=excluded.tool_result,
                    tool_use_id=excluded.tool_use_id,
                    timestamp=excluded.timestamp,
                    raw_json=excluded.raw_json
                """

                self.db.execute(
                    query,
                    (
                        session_id,
                        msg.index,
                        msg.role,
                        msg.content,
                        msg.content_type,
                        msg.tool_name,
                        tool_input,
                        tool_result,
                        msg.tool_use_id,
                        msg.timestamp.isoformat(),
                        raw_json,
                    ),
                )
                count += 1
            return count

        try:
            return await asyncio.to_thread(_store_blocking)
        except Exception as e:
            logger.error(f"Failed to store messages for session {session_id}: {e}")
            raise

    async def get_messages(
        self,
        session_id: str,
        limit: int = 100,
        offset: int = 0,
        role: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Retrieve messages for a session.

        Args:
            session_id: Session ID
            limit: Maximum number of messages to return
            offset: Offset for pagination
            role: Optional role to filter by

        Returns:
            List of message dictionaries
        """
        query = "SELECT * FROM session_messages WHERE session_id = ?"
        params: list[Any] = [session_id]

        if role:
            query += " AND role = ?"
            params.append(role)

        query += " ORDER BY message_index ASC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = await asyncio.to_thread(self.db.fetchall, query, tuple(params))
        return [dict(row) for row in rows]

    async def get_state(self, session_id: str) -> dict[str, Any] | None:
        """
        Get processing state for a session.

        Args:
            session_id: Session ID

        Returns:
            State dictionary or None if not found
        """
        row = await asyncio.to_thread(
            self.db.fetchone,
            "SELECT * FROM session_message_state WHERE session_id = ?",
            (session_id,),
        )
        return dict(row) if row else None

    async def update_state(
        self,
        session_id: str,
        byte_offset: int,
        message_index: int,
    ) -> None:
        """
        Update processing state for a session.

        Args:
            session_id: Session ID
            byte_offset: New byte offset in source file
            message_index: Index of last processed message
        """
        # Check if session exists to avoid FOREIGN KEY constraint errors
        session_exists = await asyncio.to_thread(
            self.db.fetchone,
            "SELECT 1 FROM sessions WHERE id = ?",
            (session_id,),
        )
        if not session_exists:
            logger.debug(f"Session {session_id} not found in database, skipping state update")
            return

        sql = """
        INSERT INTO session_message_state (
            session_id, last_byte_offset, last_message_index,
            last_processed_at, updated_at
        ) VALUES (?, ?, ?, datetime('now'), datetime('now'))
        ON CONFLICT(session_id) DO UPDATE SET
            last_byte_offset=excluded.last_byte_offset,
            last_message_index=excluded.last_message_index,
            last_processed_at=excluded.last_processed_at,
            updated_at=excluded.updated_at
        """
        await asyncio.to_thread(self.db.execute, sql, (session_id, byte_offset, message_index))

    async def count_messages(self, session_id: str) -> int:
        """
        Count messages for a session.

        Args:
            session_id: Session ID

        Returns:
            Number of messages
        """
        result = await asyncio.to_thread(
            self.db.fetchone,
            "SELECT COUNT(*) as count FROM session_messages WHERE session_id = ?",
            (session_id,),
        )
        return result["count"] if result else 0

    async def get_max_message_index(self, session_id: str) -> int:
        """Get highest message_index for a session, or -1 if none exist."""
        result = await asyncio.to_thread(
            self.db.fetchone,
            "SELECT MAX(message_index) as max_idx FROM session_messages WHERE session_id = ?",
            (session_id,),
        )
        max_idx = result["max_idx"] if result else None
        return max_idx if max_idx is not None else -1

    async def get_all_counts(self) -> dict[str, int]:
        """
        Get message counts for all sessions.

        Returns:
            Dictionary mapping session_id to count
        """
        rows = await asyncio.to_thread(
            self.db.fetchall,
            "SELECT session_id, COUNT(*) as count FROM session_messages GROUP BY session_id",
        )
        return {row["session_id"]: row["count"] for row in rows}

    async def delete(self, session_id: str) -> None:
        """Delete all messages for a session.

        Args:
            session_id: Session ID whose messages to delete
        """
        await asyncio.to_thread(
            self.db.execute,
            "DELETE FROM session_messages WHERE session_id = ?",
            (session_id,),
        )

    async def search_messages(
        self,
        query_text: str,
        limit: int = 20,
        offset: int = 0,
        session_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Search messages using simple text matching.

        Args:
            query_text: Text to search for
            limit: Max results
            offset: Pagination offset
            session_id: Optional session ID to filter by
            project_id: Optional project ID to filter by

        Returns:
            List of matching messages
        """
        # Escape LIKE wildcards in query_text
        escaped_query = query_text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = "SELECT m.* FROM session_messages m"
        params: list[Any] = []
        conditions: list[str] = ["m.content LIKE ? ESCAPE '\\'"]
        params.append(f"%{escaped_query}%")

        if project_id:
            sql += " JOIN sessions s ON m.session_id = s.session_id"
            conditions.append("s.project_id = ?")
            params.append(project_id)

        if session_id:
            conditions.append("m.session_id = ?")
            params.append(session_id)

        sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY m.timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = await asyncio.to_thread(self.db.fetchall, sql, tuple(params))
        return [dict(row) for row in rows]
