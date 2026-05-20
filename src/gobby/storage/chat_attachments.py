"""Storage helpers for uploaded chat attachments."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gobby.storage.database import DatabaseProtocol

CHAT_ATTACHMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_attachments (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- Client/display identifiers intentionally do not reference server tables.
    draft_id TEXT,
    conversation_id TEXT,
    message_id TEXT,
    target_session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
    local_path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    bound_at TEXT -- Set once when an attachment is first bound to a message/session.
);

CREATE INDEX IF NOT EXISTS idx_chat_attachments_project
    ON chat_attachments(project_id);

CREATE INDEX IF NOT EXISTS idx_chat_attachments_draft
    ON chat_attachments(draft_id);

CREATE INDEX IF NOT EXISTS idx_chat_attachments_conversation
    ON chat_attachments(conversation_id);

CREATE INDEX IF NOT EXISTS idx_chat_attachments_message
    ON chat_attachments(message_id);

CREATE INDEX IF NOT EXISTS idx_chat_attachments_target_session
    ON chat_attachments(target_session_id);

CREATE INDEX IF NOT EXISTS idx_chat_attachments_local_path
    ON chat_attachments(local_path);

CREATE TRIGGER IF NOT EXISTS trg_chat_attachments_bound_at_write_once
BEFORE UPDATE OF bound_at ON chat_attachments
WHEN OLD.bound_at IS NOT NULL AND NEW.bound_at IS NOT OLD.bound_at
BEGIN
    SELECT RAISE(ABORT, 'chat_attachments.bound_at is write-once');
END;

CREATE TRIGGER IF NOT EXISTS trg_chat_attachments_updated_at_touch
AFTER UPDATE ON chat_attachments
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE chat_attachments
       SET updated_at = CURRENT_TIMESTAMP
     WHERE id = NEW.id;
END;
"""


@dataclass(frozen=True)
class ChatAttachmentRecord:
    id: str
    project_id: str
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
        """Return true when any durable owner field is set.

        Attachments may be bound to a conversation, a specific message, or a
        target session; any one of those links means the queued upload is no
        longer safe for draft deletion.
        """
        return bool(self.conversation_id or self.message_id or self.target_session_id)


def _row_to_record(row: Mapping[str, Any]) -> ChatAttachmentRecord:
    return ChatAttachmentRecord(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
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


def _fetch_attachment(
    conn: sqlite3.Connection,
    attachment_id: str,
) -> ChatAttachmentRecord | None:
    row = conn.execute(
        """
        SELECT id, project_id, draft_id, conversation_id, message_id, target_session_id,
               filename, mime_type, size_bytes, local_path, created_at, updated_at, bound_at
          FROM chat_attachments
         WHERE id = ?
        """,
        (attachment_id,),
    ).fetchone()
    return _row_to_record(row) if row else None


def content_url(attachment_id: str) -> str:
    return f"/api/chat/attachments/{attachment_id}/content"


def to_api_dict(record: ChatAttachmentRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "project_id": record.project_id,
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
    project_id: str,
    draft_id: str | None,
    filename: str,
    mime_type: str,
    size_bytes: int,
    local_path: str,
    attachment_id: str | None = None,
) -> ChatAttachmentRecord:
    now = datetime.now(UTC).isoformat()
    record_id = attachment_id or str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO chat_attachments (
                id, project_id, draft_id, filename, mime_type, size_bytes, local_path,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                project_id,
                draft_id,
                filename,
                mime_type,
                size_bytes,
                local_path,
                now,
                now,
            ),
        )
        record = _fetch_attachment(conn, record_id)
    if record is None:
        raise RuntimeError("Failed to create chat attachment metadata")
    return record


def get_attachment(db: DatabaseProtocol, attachment_id: str) -> ChatAttachmentRecord | None:
    with db.transaction() as conn:
        return _fetch_attachment(conn, attachment_id)


def get_attachments_by_ids(
    db: DatabaseProtocol, attachment_ids: list[str]
) -> list[ChatAttachmentRecord]:
    if not attachment_ids:
        return []

    unique_ids = list(dict.fromkeys(attachment_ids))
    placeholders = ",".join("?" for _ in unique_ids)
    rows = db.fetchall(
        f"""
        SELECT id, project_id, draft_id, conversation_id, message_id, target_session_id,
               filename, mime_type, size_bytes, local_path, created_at, updated_at, bound_at
          FROM chat_attachments
         WHERE id IN ({placeholders})
        """,  # nosec B608 # placeholders are generated from the validated ID count only.
        tuple(unique_ids),
    )
    records = [_row_to_record(row) for row in rows]
    by_id = {record.id: record for record in records}
    return [by_id[attachment_id] for attachment_id in unique_ids if attachment_id in by_id]


def _binding_conflict_error(
    record: ChatAttachmentRecord,
    *,
    conversation_id: str | None,
    message_id: str | None,
    target_session_id: str | None,
) -> ValueError | None:
    requested = {
        "conversation_id": conversation_id,
        "message_id": message_id,
        "target_session_id": target_session_id,
    }
    for field, requested_value in requested.items():
        existing = getattr(record, field)
        if existing is not None and existing != requested_value:
            return ValueError(f"Attachment {record.id} is already bound: {field}={existing!r}")
    return None


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
    now = datetime.now(UTC).isoformat()
    with db.transaction_immediate() as conn:
        placeholders = ",".join("?" for _ in unique_ids)
        rows = conn.execute(
            f"""
            SELECT id, project_id, draft_id, conversation_id, message_id, target_session_id,
                   filename, mime_type, size_bytes, local_path, created_at, updated_at, bound_at
              FROM chat_attachments
             WHERE id IN ({placeholders})
            """,  # nosec B608 # placeholders are generated from the validated ID count only.
            tuple(unique_ids),
        ).fetchall()
        records = [_row_to_record(row) for row in rows]
        by_id = {record.id: record for record in records}
        missing_ids = [attachment_id for attachment_id in unique_ids if attachment_id not in by_id]
        if missing_ids:
            raise ValueError(f"Unknown attachment id: {missing_ids[0]}")

        for record in records:
            conflict = _binding_conflict_error(
                record,
                conversation_id=conversation_id,
                message_id=message_id,
                target_session_id=target_session_id,
            )
            if conflict is not None:
                raise conflict

        for attachment_id in unique_ids:
            conn.execute(
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
        rows = conn.execute(
            f"""
            SELECT id, project_id, draft_id, conversation_id, message_id, target_session_id,
                   filename, mime_type, size_bytes, local_path, created_at, updated_at, bound_at
              FROM chat_attachments
             WHERE id IN ({placeholders})
            """,  # nosec B608 # placeholders are generated from the validated ID count only.
            tuple(unique_ids),
        ).fetchall()
    updated_records = [_row_to_record(row) for row in rows]
    updated_by_id = {record.id: record for record in updated_records}
    return [updated_by_id[attachment_id] for attachment_id in unique_ids]


def delete_unbound_attachment(
    db: DatabaseProtocol, attachment_id: str
) -> ChatAttachmentRecord | None:
    with db.transaction_immediate() as conn:
        row = conn.execute(
            """
            DELETE FROM chat_attachments
             WHERE id = ?
               AND conversation_id IS NULL
               AND message_id IS NULL
               AND target_session_id IS NULL
            RETURNING id, project_id, draft_id, conversation_id, message_id, target_session_id,
                      filename, mime_type, size_bytes, local_path, created_at, updated_at, bound_at
            """,
            (attachment_id,),
        ).fetchone()
        if row is not None:
            return _row_to_record(row)

        record = _fetch_attachment(conn, attachment_id)
        if record is not None and record.is_bound:
            raise ValueError("Only unbound queued attachments can be deleted")
        return None


def delete_stale_unbound_attachments(
    db: DatabaseProtocol,
    *,
    cutoff: datetime | str,
    limit: int = 500,
) -> list[ChatAttachmentRecord]:
    """Delete never-bound queued uploads older than ``cutoff`` and return their records."""
    cutoff_value = cutoff.isoformat() if isinstance(cutoff, datetime) else cutoff
    bounded_limit = max(1, int(limit))
    with db.transaction_immediate() as conn:
        rows = conn.execute(
            """
            DELETE FROM chat_attachments
             WHERE id IN (
                   SELECT id
                     FROM chat_attachments
                    WHERE conversation_id IS NULL
                      AND message_id IS NULL
                      AND target_session_id IS NULL
                      AND bound_at IS NULL
                      AND created_at < ?
                    ORDER BY created_at ASC
                    LIMIT ?
             )
            RETURNING id, project_id, draft_id, conversation_id, message_id, target_session_id,
                      filename, mime_type, size_bytes, local_path, created_at, updated_at, bound_at
            """,
            (cutoff_value, bounded_limit),
        ).fetchall()
    return [_row_to_record(row) for row in rows]


def delete_attachments_for_conversations(
    db: DatabaseProtocol,
    conversation_ids: list[str],
) -> list[ChatAttachmentRecord]:
    """Delete attachment metadata tied to conversations or their chat messages."""
    unique_ids = [value for value in dict.fromkeys(conversation_ids) if value]
    if not unique_ids:
        return []

    placeholders = ",".join("?" for _ in unique_ids)
    with db.transaction_immediate() as conn:
        rows = conn.execute(
            f"""
            SELECT id, project_id, draft_id, conversation_id, message_id, target_session_id,
                   filename, mime_type, size_bytes, local_path, created_at, updated_at, bound_at
              FROM chat_attachments
             WHERE conversation_id IN ({placeholders})
                OR message_id IN (
                    SELECT id
                      FROM chat_messages
                     WHERE conversation_id IN ({placeholders})
                )
            """,  # nosec B608 # placeholders are generated from the conversation ID count only.
            (*unique_ids, *unique_ids),
        ).fetchall()
        records = [_row_to_record(row) for row in rows]
        if not records:
            return []
        attachment_ids = [record.id for record in records]
        id_placeholders = ",".join("?" for _ in attachment_ids)
        conn.execute(
            f"DELETE FROM chat_attachments WHERE id IN ({id_placeholders})",
            tuple(attachment_ids),
        )  # nosec B608 # placeholders are generated from selected attachment rows only.
        return records
