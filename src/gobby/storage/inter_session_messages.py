"""Inter-session messaging for agent coordination.

This module provides storage and management of messages sent between sessions,
enabling parent-child session communication and agent coordination.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from gobby.utils.datetime import normalize_datetime_model, to_aware_utc, utc_now

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

from gobby.storage.sql_dialect import json_text_expr

MESSAGE_DIRECTION_ALIASES: dict[str, str] = {
    "all": "all",
    "inbox": "inbox",
    "received": "inbox",
    "sent": "sent",
}
MESSAGE_DIRECTION_OPTIONS = tuple(MESSAGE_DIRECTION_ALIASES)


def normalize_message_direction(direction: str) -> str:
    """Normalize public message history direction values."""
    if not isinstance(direction, str):
        raise ValueError(
            f"Invalid direction. Expected one of: {', '.join(MESSAGE_DIRECTION_OPTIONS)}"
        )
    normalized = MESSAGE_DIRECTION_ALIASES.get(direction.strip().lower())
    if normalized is None:
        raise ValueError(
            f"Invalid direction '{direction}'. Expected one of: "
            f"{', '.join(MESSAGE_DIRECTION_OPTIONS)}"
        )
    return normalized


@normalize_datetime_model(
    required=("sent_at",),
    optional=("delivered_at",),
)
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
    """

    id: str
    from_session: str
    to_session: str
    content: str
    priority: str
    sent_at: datetime
    message_type: str = "message"
    metadata_json: str | None = None
    delivered_at: datetime | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> InterSessionMessage:
        """Create instance from database row.

        Args:
            row: Database row with message data

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
        }


class InterSessionMessageManager:
    """Manages inter-session messages.

    Provides CRUD operations for messages sent between sessions,
    enabling agent coordination and parent-child communication.
    """

    def __init__(self, db: HubDatabase) -> None:
        """Initialize the message manager.

        Args:
            db: Hub database instance for persistence
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
        sent_at = utc_now()

        self.db.execute(
            """
            INSERT INTO inter_session_messages
            (id, from_session, to_session, content, priority, sent_at,
             message_type, metadata_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
            "SELECT * FROM inter_session_messages WHERE id = %s",
            (message_id,),
        )

        if row:
            return InterSessionMessage.from_row(row)
        return None

    def get_messages(self, to_session: str) -> list[InterSessionMessage]:
        """Get messages for a recipient session.

        Args:
            to_session: ID of the receiving session
        Returns:
            List of InterSessionMessage instances
        """
        query = """SELECT * FROM inter_session_messages
                   WHERE to_session = %s
                   ORDER BY sent_at ASC, id ASC"""

        rows = self.db.fetchall(query, (to_session,))
        return [InterSessionMessage.from_row(row) for row in rows]

    def has_completion_notification(
        self,
        to_session: str,
        message_type: str,
        completion_id: str,
    ) -> bool:
        """Return True when a completion notification already exists."""
        completion_id_sql = json_text_expr(self.db, "metadata_json", "completion_id")
        run_id_sql = json_text_expr(self.db, "metadata_json", "run_id")
        execution_id_sql = json_text_expr(self.db, "metadata_json", "execution_id")
        row = self.db.fetchone(
            f"""
            SELECT 1 FROM inter_session_messages
            WHERE to_session = %s
              AND message_type = %s
              AND metadata_json IS NOT NULL
              AND (
                {completion_id_sql} = %s
                OR {run_id_sql} = %s
                OR {execution_id_sql} = %s
              )
            LIMIT 1
            """,  # nosec B608 # JSON expressions are generated from static keys.
            (to_session, message_type, completion_id, completion_id, completion_id),
        )
        return row is not None

    def get_undelivered_messages(self, to_session: str) -> list[InterSessionMessage]:
        """Get messages not yet delivered to a session.

        Args:
            to_session: ID of the receiving session

        Returns:
            List of undelivered InterSessionMessage instances
        """
        rows = self.db.fetchall(
            """SELECT * FROM inter_session_messages
               WHERE to_session = %s AND delivered_at IS NULL
               ORDER BY sent_at""",
            (to_session,),
        )
        return [InterSessionMessage.from_row(row) for row in rows]

    def mark_delivered_batch(
        self,
        message_ids: list[str],
        to_session: str,
    ) -> list[str]:
        """Atomically mark selected messages delivered and return the updated IDs."""
        if not message_ids:
            return []

        unique_ids = list(dict.fromkeys(message_ids))
        placeholders = ",".join("%s" for _ in unique_ids)
        delivered_at = utc_now()
        rows = self.db.fetchall(
            f"""UPDATE inter_session_messages
                SET delivered_at = %s
                WHERE to_session = %s
                  AND delivered_at IS NULL
                  AND id IN ({placeholders})
                RETURNING id""",  # nosec B608
            (delivered_at, to_session, *unique_ids),
        )
        return [str(row["id"]) for row in rows]

    def delete_delivered_before(self, cutoff: datetime, *, limit: int = 500) -> int:
        """Delete a bounded batch of delivered messages older than a cutoff."""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                """DELETE FROM inter_session_messages
                   WHERE id IN (
                       SELECT id FROM inter_session_messages
                       WHERE delivered_at < %s
                       ORDER BY delivered_at ASC, id ASC
                       LIMIT %s
                   )""",
                (to_aware_utc(cutoff), limit),
            )
            return cursor.rowcount

    def list_messages(
        self,
        session_id: str,
        direction: str = "all",
        undelivered_only: bool = False,
        message_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[InterSessionMessage]:
        """List messages for a session with flexible filtering.

        Read-only query — does not mark messages as read or delivered.

        Args:
            session_id: Session to query messages for
            direction: "inbox"/"received", "sent", or "all"
            undelivered_only: If True, only return messages with delivered_at IS NULL
            message_type: Filter by message_type (e.g. "message", "command_result")
            limit: Max rows to return (default 50)
            offset: Rows to skip for pagination (default 0)

        Returns:
            List of InterSessionMessage instances ordered by sent_at DESC
        """
        direction = normalize_message_direction(direction)

        conditions: list[str] = []
        params: list[Any] = []

        if direction == "inbox":
            conditions.append("to_session = %s")
            params.append(session_id)
        elif direction == "sent":
            conditions.append("from_session = %s")
            params.append(session_id)
        else:  # "all"
            conditions.append("(from_session = %s OR to_session = %s)")
            params.extend([session_id, session_id])

        if undelivered_only:
            conditions.append("delivered_at IS NULL")
        if message_type is not None:
            conditions.append("message_type = %s")
            params.append(message_type)

        where = " AND ".join(conditions)
        query = (
            f"SELECT * FROM inter_session_messages WHERE {where} "
            f"ORDER BY sent_at DESC LIMIT %s OFFSET %s"
        )
        params.extend([limit, offset])

        rows = self.db.fetchall(query, tuple(params))
        return [InterSessionMessage.from_row(row) for row in rows]

    def mark_delivered(self, message_id: str, to_session: str) -> InterSessionMessage:
        """Mark a message as delivered.

        Args:
            message_id: The message ID to mark as delivered
            to_session: ID of the receiving session

        Returns:
            The updated InterSessionMessage

        Raises:
            ValueError: If message not found
        """
        delivered_at = utc_now()

        row = self.db.fetchone(
            """UPDATE inter_session_messages
               SET delivered_at = %s
               WHERE id = %s AND to_session = %s AND delivered_at IS NULL
               RETURNING *""",
            (delivered_at, message_id, to_session),
        )
        if not row:
            raise ValueError(
                f"Undelivered message not found for recipient {to_session}: {message_id}"
            )
        return InterSessionMessage.from_row(row)
