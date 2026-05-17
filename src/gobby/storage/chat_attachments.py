"""Storage helpers for uploaded chat attachments."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gobby.storage.database import DatabaseProtocol

CHAT_ATTACHMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_attachments (
    id TEXT PRIMARY KEY,
    draft_id TEXT,
    conversation_id TEXT,
    message_id TEXT,
    target_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    local_path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    bound_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_chat_attachments_draft
    ON chat_attachments(draft_id);

CREATE INDEX IF NOT EXISTS idx_chat_attachments_conversation
    ON chat_attachments(conversation_id);

CREATE INDEX IF NOT EXISTS idx_chat_attachments_message
    ON chat_attachments(message_id);

CREATE INDEX IF NOT EXISTS idx_chat_attachments_target_session
    ON chat_attachments(target_session_id);
"""


@dataclass(frozen=True)
class ChatAttachmentRecord:
    id: str
    draft_id: str | None
    conversation_id: str | None
    message_id: str | None
    target_session_id: str | None
    filename: str
    mime_type: str
    size_bytes: int
    local_path: str
    created_at: str
    updated_at: str
    bound_at: str | None

    @property
    def is_bound(self) -> bool:
        return bool(self.conversation_id or self.message_id or self.target_session_id)


def _row_to_record(row: sqlite3.Row) -> ChatAttachmentRecord:
    return ChatAttachmentRecord(
        id=str(row["id"]),
        draft_id=row["draft_id"],
        conversation_id=row["conversation_id"],
        message_id=row["message_id"],
        target_session_id=row["target_session_id"],
        filename=str(row["filename"]),
        mime_type=str(row["mime_type"]),
        size_bytes=int(row["size_bytes"]),
        local_path=str(row["local_path"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        bound_at=row["bound_at"],
    )


def content_url(attachment_id: str) -> str:
    return f"/api/chat/attachments/{attachment_id}/content"


def to_api_dict(record: ChatAttachmentRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "draft_id": record.draft_id,
        "conversation_id": record.conversation_id,
        "message_id": record.message_id,
        "target_session_id": record.target_session_id,
        "filename": record.filename,
        "mime_type": record.mime_type,
        "size_bytes": record.size_bytes,
        "content_url": content_url(record.id),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "bound_at": record.bound_at,
    }


def create_attachment(
    db: DatabaseProtocol,
    *,
    draft_id: str | None,
    filename: str,
    mime_type: str,
    size_bytes: int,
    local_path: str,
    attachment_id: str | None = None,
) -> ChatAttachmentRecord:
    now = datetime.now(UTC).isoformat()
    record_id = attachment_id or str(uuid.uuid4())
    db.execute(
        """
        INSERT INTO chat_attachments (
            id, draft_id, filename, mime_type, size_bytes, local_path, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (record_id, draft_id, filename, mime_type, size_bytes, local_path, now, now),
    )
    record = get_attachment(db, record_id)
    if record is None:
        raise RuntimeError("Failed to create chat attachment metadata")
    return record


def get_attachment(db: DatabaseProtocol, attachment_id: str) -> ChatAttachmentRecord | None:
    row = db.fetchone(
        """
        SELECT id, draft_id, conversation_id, message_id, target_session_id,
               filename, mime_type, size_bytes, local_path, created_at, updated_at, bound_at
          FROM chat_attachments
         WHERE id = ?
        """,
        (attachment_id,),
    )
    return _row_to_record(row) if row else None


def get_attachments_by_ids(
    db: DatabaseProtocol, attachment_ids: list[str]
) -> list[ChatAttachmentRecord]:
    if not attachment_ids:
        return []

    unique_ids = list(dict.fromkeys(attachment_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    rows = db.fetchall(
        f"""
        SELECT id, draft_id, conversation_id, message_id, target_session_id,
               filename, mime_type, size_bytes, local_path, created_at, updated_at, bound_at
          FROM chat_attachments
         WHERE id IN ({placeholders})
        """,  # nosec B608 # placeholders are generated from the validated ID count only.
        tuple(unique_ids),
    )
    by_id = {_row_to_record(row).id: _row_to_record(row) for row in rows}
    return [by_id[attachment_id] for attachment_id in unique_ids if attachment_id in by_id]


def bind_attachments(
    db: DatabaseProtocol,
    attachment_ids: list[str],
    *,
    conversation_id: str | None = None,
    message_id: str | None = None,
    target_session_id: str | None = None,
) -> list[ChatAttachmentRecord]:
    if not attachment_ids:
        return []

    unique_ids = list(dict.fromkeys(attachment_ids))
    records = get_attachments_by_ids(db, unique_ids)
    found_ids = {record.id for record in records}
    missing_ids = [attachment_id for attachment_id in unique_ids if attachment_id not in found_ids]
    if missing_ids:
        raise ValueError(f"Unknown attachment id: {missing_ids[0]}")

    for record in records:
        same_conversation = record.conversation_id in (None, conversation_id)
        same_message = record.message_id in (None, message_id)
        same_target = record.target_session_id in (None, target_session_id)
        if not (same_conversation and same_message and same_target):
            raise ValueError(f"Attachment {record.id} is already bound")

    now = datetime.now(UTC).isoformat()
    with db.transaction():
        for attachment_id in unique_ids:
            db.execute(
                """
                UPDATE chat_attachments
                   SET conversation_id = COALESCE(?, conversation_id),
                       message_id = COALESCE(?, message_id),
                       target_session_id = COALESCE(?, target_session_id),
                       bound_at = COALESCE(bound_at, ?),
                       updated_at = ?
                 WHERE id = ?
                """,
                (conversation_id, message_id, target_session_id, now, now, attachment_id),
            )
    return get_attachments_by_ids(db, unique_ids)


def delete_unbound_attachment(
    db: DatabaseProtocol, attachment_id: str
) -> ChatAttachmentRecord | None:
    record = get_attachment(db, attachment_id)
    if record is None:
        return None
    if record.is_bound:
        raise ValueError("Only unbound queued attachments can be deleted")
    db.execute("DELETE FROM chat_attachments WHERE id = ?", (attachment_id,))
    return record
