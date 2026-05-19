"""Storage module for web chat display messages.

Persists chat messages server-side so they survive page refreshes
and are available across devices. The Claude SDK transcript handles
AI context; this table handles display-layer persistence.
"""

import json
import logging
import uuid
from typing import Any

from gobby.storage.database import LocalDatabase

logger = logging.getLogger(__name__)


def save_message(
    db: LocalDatabase,
    *,
    conversation_id: str,
    role: str,
    content: str,
    tool_calls_json: str | None = None,
    content_blocks_json: str | None = None,
    metadata_json: str | None = None,
    seq: int | None = None,
) -> str:
    """Save a chat message. Returns the message ID."""
    msg_id = str(uuid.uuid4())
    with db.transaction() as conn:
        if seq is None:
            row = conn.execute(
                """
                SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq
                  FROM chat_messages
                 WHERE conversation_id = ?
                """,
                (conversation_id,),
            ).fetchone()
            seq = row["next_seq"]
        conn.execute(
            """INSERT INTO chat_messages (
                   id, conversation_id, role, content, tool_calls_json,
                   content_blocks_json, metadata_json, seq
               )
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg_id,
                conversation_id,
                role,
                content,
                tool_calls_json,
                content_blocks_json,
                metadata_json,
                seq,
            ),
        )
    return msg_id


def get_messages(
    db: LocalDatabase,
    conversation_id: str,
    *,
    after_seq: int = 0,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Load messages for a conversation, optionally after a sequence number."""
    rows = db.fetchall(
        """SELECT id, conversation_id, role, content, tool_calls_json, content_blocks_json,
                  metadata_json, seq, created_at
           FROM chat_messages
           WHERE conversation_id = ? AND seq > ?
           ORDER BY seq ASC
           LIMIT ?""",
        (conversation_id, after_seq, limit),
    )
    result = []
    for row in rows:
        msg: dict[str, Any] = {
            "id": row["id"],
            "conversation_id": row["conversation_id"],
            "role": row["role"],
            "content": row["content"],
            "seq": row["seq"],
            "created_at": row["created_at"],
        }
        if row["tool_calls_json"]:
            try:
                msg["tool_calls"] = json.loads(row["tool_calls_json"])
            except json.JSONDecodeError:
                msg["tool_calls"] = []
        if row["content_blocks_json"]:
            try:
                msg["content_blocks"] = json.loads(row["content_blocks_json"])
            except json.JSONDecodeError:
                msg["content_blocks"] = []
        if row["metadata_json"]:
            try:
                msg["metadata"] = json.loads(row["metadata_json"])
            except json.JSONDecodeError:
                msg["metadata"] = {}
        result.append(msg)
    return result


def delete_messages(db: LocalDatabase, conversation_id: str) -> int:
    """Delete all messages for a conversation. Returns count deleted."""
    with db.transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM chat_messages WHERE conversation_id = ?",
            (conversation_id,),
        )
        return cursor.rowcount


def get_max_seq(db: LocalDatabase, conversation_id: str) -> int:
    """Get the maximum sequence number for a conversation."""
    row = db.fetchone(
        "SELECT MAX(seq) as max_seq FROM chat_messages WHERE conversation_id = ?",
        (conversation_id,),
    )
    return row["max_seq"] if row and row["max_seq"] is not None else 0
