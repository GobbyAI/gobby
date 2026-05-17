"""Inter-session messaging for agent coordination.

This module provides storage and management of messages sent between sessions,
enabling parent-child session communication and agent coordination.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from sqlite3 import Row
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.storage.database import DatabaseProtocol, LocalDatabase


@dataclass
class InterSessionMessage:
    """A message sent between sessions.

    Attributes:
        id: Unique message identifier
        from_session: ID of the sending session
        to_session: ID of the receiving session
        content: Message content
        priority: Message priority (e.g., "normal", "urgent")
        sent_at: Timestamp when message was sent
        read_at: Timestamp when message was read (None if unread)
    """

    id: str
    from_session: str
    to_session: str
    content: str
    priority: str
    sent_at: str
    read_at: str | None
    message_type: str = "message"
    metadata_json: str | None = None
    delivered_at: str | None = None

    @classmethod
    def from_row(cls, row: Row) -> InterSessionMessage:
        """Create instance from database row.

        Args:
            row: SQLite row with message data

        Returns:
            InterSessionMessage instance
        """
        return cls(
            id=row["id"],
            from_session=row["from_session"],
            to_session=row["to_session"],
            content=row["content"],
            priority=row["priority"],
            sent_at=row["sent_at"],
            read_at=row["read_at"],
            message_type=row["message_type"],
            metadata_json=row["metadata_json"],
            delivered_at=row["delivered_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary.

        Returns:
            Dictionary with all message fields
        """
        return {
            "id": self.id,
            "from_session": self.from_session,
            "to_session": self.to_session,
            "content": self.content,
            "priority": self.priority,
            "sent_at": self.sent_at,
            "read_at": self.read_at,
            "message_type": self.message_type,
            "metadata_json": self.metadata_json,
            "delivered_at": self.delivered_at,
        }

    def to_brief(self) -> dict[str, Any]:
        """Slim representation for list operations."""
        return {
            "id": self.id,
            "from_session": self.from_session,
            "to_session": self.to_session,
            "content": self.content,
            "priority": self.priority,
            "message_type": self.message_type,
            "sent_at": self.sent_at,
            "read_at": self.read_at,
        }


class InterSessionMessageManager:
    """Manages inter-session messages.

    Provides CRUD operations for messages sent between sessions,
    enabling agent coordination and parent-child communication.
    """

    def __init__(self, db: LocalDatabase | DatabaseProtocol) -> None:
        """Initialize the message manager.

        Args:
            db: LocalDatabase instance for persistence
        """
        self.db = db

    def create_message(
        self,
        from_session: str,
        to_session: str,
        content: str,
        priority: str = "normal",
        message_type: str = "message",
        metadata_json: str | None = None,
    ) -> InterSessionMessage:
        """Create and persist a new message.

        Args:
            from_session: ID of the sending session
            to_session: ID of the receiving session
            content: Message content
            priority: Message priority (default: "normal")
            message_type: Message type (default: "message")
            metadata_json: Optional JSON metadata string

        Returns:
            The created InterSessionMessage
        """
        message_id = str(uuid.uuid4())
        sent_at = datetime.now(UTC).isoformat()

        self.db.execute(
            """
            INSERT INTO inter_session_messages
            (id, from_session, to_session, content, priority, sent_at, read_at,
             message_type, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                message_id,
                from_session,
                to_session,
                content,
                priority,
                sent_at,
                message_type,
                metadata_json,
            ),
        )

        return InterSessionMessage(
            id=message_id,
            from_session=from_session,
            to_session=to_session,
            content=content,
            priority=priority,
            sent_at=sent_at,
            read_at=None,
            message_type=message_type,
            metadata_json=metadata_json,
        )

    def get_message(self, message_id: str) -> InterSessionMessage | None:
        """Get a message by ID.

        Args:
            message_id: The message ID to retrieve

        Returns:
            The InterSessionMessage if found, None otherwise
        """
        row = self.db.fetchone(
            "SELECT * FROM inter_session_messages WHERE id = ?",
            (message_id,),
        )

        if row:
            return InterSessionMessage.from_row(row)
        return None

    def get_messages(self, to_session: str, unread_only: bool = False) -> list[InterSessionMessage]:
        """Get messages for a recipient session.

        Args:
            to_session: ID of the receiving session
            unread_only: If True, only return unread messages

        Returns:
            List of InterSessionMessage instances
        """
        if unread_only:
            query = """
                SELECT * FROM inter_session_messages
                WHERE to_session = ? AND read_at IS NULL
            """
        else:
            query = "SELECT * FROM inter_session_messages WHERE to_session = ?"

        rows = self.db.fetchall(query, (to_session,))
        return [InterSessionMessage.from_row(row) for row in rows]

    def has_completion_notification(
        self,
        to_session: str,
        message_type: str,
        completion_id: str,
    ) -> bool:
        """Return True when a completion notification already exists."""
        row = self.db.fetchone(
            """
            SELECT 1 FROM inter_session_messages
            WHERE to_session = ?
              AND message_type = ?
              AND metadata_json IS NOT NULL
              AND json_valid(metadata_json)
              AND (
                json_extract(metadata_json, '$.completion_id') = ?
                OR json_extract(metadata_json, '$.run_id') = ?
                OR json_extract(metadata_json, '$.execution_id') = ?
              )
            LIMIT 1
            """,
            (to_session, message_type, completion_id, completion_id, completion_id),
        )
        return row is not None

    def mark_read(self, message_id: str) -> InterSessionMessage:
        """Mark a message as read.

        Args:
            message_id: The message ID to mark as read

        Returns:
            The updated InterSessionMessage

        Raises:
            ValueError: If message not found
        """
        read_at = datetime.now(UTC).isoformat()

        self.db.execute(
            "UPDATE inter_session_messages SET read_at = ? WHERE id = ?",
            (read_at, message_id),
        )

        message = self.get_message(message_id)
        if not message:
            raise ValueError(f"Message not found: {message_id}")
        return message

    def get_undelivered_messages(self, to_session: str) -> list[InterSessionMessage]:
        """Get messages not yet delivered to a session.

        Args:
            to_session: ID of the receiving session

        Returns:
            List of undelivered InterSessionMessage instances
        """
        rows = self.db.fetchall(
            """SELECT * FROM inter_session_messages
               WHERE to_session = ? AND delivered_at IS NULL
               ORDER BY sent_at""",
            (to_session,),
        )
        return [InterSessionMessage.from_row(row) for row in rows]

    def list_messages(
        self,
        session_id: str,
        direction: str = "all",
        unread_only: bool = False,
        undelivered_only: bool = False,
        message_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[InterSessionMessage]:
        """List messages for a session with flexible filtering.

        Read-only query — does not mark messages as read or delivered.

        Args:
            session_id: Session to query messages for
            direction: "inbox" (received), "sent", or "all"
            unread_only: If True, only return messages with read_at IS NULL
            undelivered_only: If True, only return messages with delivered_at IS NULL
            message_type: Filter by message_type (e.g. "message", "command_result")
            limit: Max rows to return (default 50)
            offset: Rows to skip for pagination (default 0)

        Returns:
            List of InterSessionMessage instances ordered by sent_at DESC
        """
        conditions: list[str] = []
        params: list[Any] = []

        if direction == "inbox":
            conditions.append("to_session = ?")
            params.append(session_id)
        elif direction == "sent":
            conditions.append("from_session = ?")
            params.append(session_id)
        else:  # "all"
            conditions.append("(from_session = ? OR to_session = ?)")
            params.extend([session_id, session_id])

        if unread_only:
            conditions.append("read_at IS NULL")
        if undelivered_only:
            conditions.append("delivered_at IS NULL")
        if message_type is not None:
            conditions.append("message_type = ?")
            params.append(message_type)

        where = " AND ".join(conditions)
        query = (
            f"SELECT * FROM inter_session_messages WHERE {where} "
            f"ORDER BY sent_at DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        rows = self.db.fetchall(query, tuple(params))
        return [InterSessionMessage.from_row(row) for row in rows]

    def mark_delivered(self, message_id: str) -> InterSessionMessage:
        """Mark a message as delivered.

        Args:
            message_id: The message ID to mark as delivered

        Returns:
            The updated InterSessionMessage

        Raises:
            ValueError: If message not found
        """
        delivered_at = datetime.now(UTC).isoformat()

        self.db.execute(
            "UPDATE inter_session_messages SET delivered_at = ? WHERE id = ?",
            (delivered_at, message_id),
        )

        message = self.get_message(message_id)
        if not message:
            raise ValueError(f"Message not found: {message_id}")
        return message
