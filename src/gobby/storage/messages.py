"""
Local storage for session messages.
"""

import json
import logging
from typing import Any

from gobby.sessions.transcripts.base import ParsedMessage
from gobby.storage.database import LocalDatabase

logger = logging.getLogger(__name__)


class LocalMessageManager:
    """Manages storage of session messages and processing state."""

    def __init__(self, database: LocalDatabase):
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

        count = 0
        try:
            for msg in messages:
                # Convert dicts to JSON strings for storage
                tool_input = json.dumps(msg.tool_input) if msg.tool_input else None
                tool_result = json.dumps(msg.tool_result) if msg.tool_result else None
                raw_json = json.dumps(msg.raw_json) if msg.raw_json else None

                query = """
                INSERT INTO session_messages (
                    session_id, message_index, role, content, content_type,
                    tool_name, tool_input, tool_result, timestamp, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, message_index) DO UPDATE SET
                    content=excluded.content,
                    tool_result=excluded.tool_result,
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
                        msg.timestamp.isoformat(),
                        raw_json,
                    ),
                )
                count += 1

            return count

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

        rows = self.db.fetchall(query, tuple(params))
        return [dict(row) for row in rows]

    async def get_state(self, session_id: str) -> dict[str, Any] | None:
        """
        Get processing state for a session.

        Args:
            session_id: Session ID

        Returns:
            State dictionary or None if not found
        """
        row = self.db.fetchone(
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
        query = """
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
        self.db.execute(query, (session_id, byte_offset, message_index))
