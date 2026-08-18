"""Storage helpers for uploaded chat attachments."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from gobby.storage.chat_attachment_fence import (
    CleanupFenceConflict,
    lock_producer_scopes,
)
from gobby.storage.hub.protocol import ChatAttachmentMutation, HubDatabase, Transaction
from gobby.utils.datetime import (
    normalize_datetime_model,
    parse_stored_datetime,
    require_stored_datetime,
    utc_now,
)
from gobby.utils.machine_id import require_machine_id

_ATTACHMENT_COLUMNS = """
id, machine_id, project_id, draft_id, conversation_id, message_id,
target_session_id, filename, mime_type, size_bytes, local_path,
created_at, updated_at, bound_at, published, claim_token, claimed_at
"""


@normalize_datetime_model(
    required=(
        "created_at",
        "updated_at",
    ),
    optional=("bound_at",),
)
@dataclass(frozen=True)
class ChatAttachmentRecord:
    id: str
    machine_id: str
    project_id: str
    draft_id: str | None
    conversation_id: str | None
    message_id: str | None
    target_session_id: str | None
    filename: str
    mime_type: str
    size_bytes: int
    local_path: str
    created_at: datetime
    updated_at: datetime
    bound_at: datetime | None
    published: bool = True
    claim_token: str | None = None
    claimed_at: datetime | None = None

    @property
    def is_bound(self) -> bool:
        """Return true when any durable owner field is set.

        Attachments may be bound to a conversation, a specific message, or a
        target session; any one of those links means the queued upload is no
        longer safe for draft deletion.
        """
        return bool(self.conversation_id or self.message_id or self.target_session_id)


def _optional_row_str(row: Mapping[str, object], key: str) -> str | None:
    value = row[key]
    return None if value is None else str(value)


def _row_int(row: Mapping[str, object], key: str) -> int:
    value = row[key]
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise TypeError(f"{key} must be int-compatible, got {type(value).__name__}")


def _row_datetime(row: Mapping[str, object], key: str) -> datetime | str | None:
    value = row.get(key)
    if value is None or isinstance(value, datetime | str):
        return value
    raise TypeError(f"{key} must be datetime-compatible, got {type(value).__name__}")


def _row_to_record(row: Mapping[str, object]) -> ChatAttachmentRecord:
    return ChatAttachmentRecord(
        id=str(row["id"]),
        machine_id=str(row["machine_id"]),
        project_id=str(row["project_id"]),
        draft_id=_optional_row_str(row, "draft_id"),
        conversation_id=_optional_row_str(row, "conversation_id"),
        message_id=_optional_row_str(row, "message_id"),
        target_session_id=_optional_row_str(row, "target_session_id"),
        filename=str(row["filename"]),
        mime_type=str(row["mime_type"]),
        size_bytes=_row_int(row, "size_bytes"),
        local_path=str(row["local_path"]),
        created_at=require_stored_datetime(_row_datetime(row, "created_at"), "created_at"),
        updated_at=require_stored_datetime(_row_datetime(row, "updated_at"), "updated_at"),
        bound_at=parse_stored_datetime(_row_datetime(row, "bound_at")),
        published=bool(row.get("published", True)),
        claim_token=_optional_row_str(row, "claim_token") if "claim_token" in row else None,
        claimed_at=parse_stored_datetime(_row_datetime(row, "claimed_at"))
        if "claimed_at" in row
        else None,
    )


def _fetch_attachment(
    conn: Transaction,
    attachment_id: str,
    project_id: str | None = None,
) -> ChatAttachmentRecord | None:
    if project_id is None:
        row = conn.execute(
            f"""
            SELECT {_ATTACHMENT_COLUMNS}
              FROM chat_attachments
             WHERE id = %s
            """,
            (attachment_id,),
        ).fetchone()
        if row is None:
            return None
        return _fetch_attachment(conn, attachment_id, str(row["project_id"]))
    row = conn.execute(
        f"""
        SELECT {_ATTACHMENT_COLUMNS}
          FROM chat_attachments
         WHERE id = %s
           AND project_id = %s
        """,
        (attachment_id, project_id),
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
    db: HubDatabase,
    *,
    project_id: str,
    draft_id: str | None,
    filename: str,
    mime_type: str,
    size_bytes: int,
    local_path: str,
    attachment_id: str | None = None,
    published: bool = False,
    claim_token: str | None = None,
) -> ChatAttachmentRecord:
    now = utc_now()
    record_id = attachment_id or str(uuid.uuid4())
    machine_id = require_machine_id()
    claimed_at = now if claim_token else None
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO chat_attachments (
                id, machine_id, project_id, draft_id, filename, mime_type, size_bytes,
                local_path, created_at, updated_at, published, claim_token, claimed_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                record_id,
                machine_id,
                project_id,
                draft_id,
                filename,
                mime_type,
                size_bytes,
                local_path,
                now,
                now,
                published,
                claim_token,
                claimed_at,
            ),
        )
        record = _fetch_attachment(conn, record_id, project_id)
    if record is None:
        raise RuntimeError("Failed to create chat attachment metadata")
    return record


def get_attachment(
    db: HubDatabase,
    attachment_id: str,
    *,
    require_published: bool = True,
) -> ChatAttachmentRecord | None:
    with db.transaction() as conn:
        record = _fetch_attachment(conn, attachment_id)
    if record is None:
        return None
    if require_published and not record.published:
        return None
    return record


def get_attachments_by_ids(
    db: HubDatabase,
    attachment_ids: list[str],
    *,
    require_published: bool = True,
) -> list[ChatAttachmentRecord]:
    if not attachment_ids:
        return []

    unique_ids = list(dict.fromkeys(attachment_ids))
    placeholders = ",".join("%s" for _ in unique_ids)
    rows = db.fetchall(
        f"""
        SELECT {_ATTACHMENT_COLUMNS}
          FROM chat_attachments
         WHERE id IN ({placeholders})
        """,  # nosec B608 # placeholders are generated from the validated ID count only.
        tuple(unique_ids),
    )
    records = [_row_to_record(row) for row in rows]
    if require_published:
        records = [record for record in records if record.published]
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
    db: HubDatabase,
    attachment_ids: list[str],
    *,
    conversation_id: str | None = None,
    message_id: str | None = None,
    target_session_id: str | None = None,
) -> list[ChatAttachmentRecord]:
    if not attachment_ids:
        return []

    unique_ids = list(dict.fromkeys(attachment_ids))
    now = utc_now()
    with db.transaction_immediate(ChatAttachmentMutation()) as conn:
        try:
            lock_producer_scopes(
                conn,
                conversation_id=conversation_id,
                target_session_id=target_session_id,
            )
        except CleanupFenceConflict as exc:
            raise ValueError(str(exc)) from exc
        placeholders = ",".join("%s" for _ in unique_ids)
        rows = conn.execute(
            f"""
            SELECT {_ATTACHMENT_COLUMNS}
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
            if not record.published or record.claim_token is not None:
                raise ValueError(f"Unknown attachment id: {record.id}")
            conflict = _binding_conflict_error(
                record,
                conversation_id=conversation_id,
                message_id=message_id,
                target_session_id=target_session_id,
            )
            if conflict is not None:
                raise conflict

        for record in records:
            cursor = conn.execute(
                """
                UPDATE chat_attachments
                   SET conversation_id = COALESCE(%s, conversation_id),
                       message_id = COALESCE(%s, message_id),
                       target_session_id = COALESCE(%s, target_session_id),
                       bound_at = COALESCE(bound_at, %s),
                       updated_at = %s
                 WHERE id = %s
                   AND project_id = %s
                   AND claim_token IS NULL
                   AND published IS TRUE
                """,
                (
                    conversation_id,
                    message_id,
                    target_session_id,
                    now,
                    now,
                    record.id,
                    record.project_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown attachment id: {record.id}")
        rows = conn.execute(
            f"""
            SELECT {_ATTACHMENT_COLUMNS}
              FROM chat_attachments
             WHERE id IN ({placeholders})
            """,  # nosec B608 # placeholders are generated from the validated ID count only.
            tuple(unique_ids),
        ).fetchall()
    updated_records = [_row_to_record(row) for row in rows]
    updated_by_id = {record.id: record for record in updated_records}
    return [updated_by_id[attachment_id] for attachment_id in unique_ids]


def delete_attachment_row(db: HubDatabase, *, attachment_id: str, project_id: str) -> bool:
    with db.transaction_immediate(ChatAttachmentMutation()) as conn:
        cursor = conn.execute(
            "DELETE FROM chat_attachments WHERE id = %s AND project_id = %s",
            (attachment_id, project_id),
        )
        return cursor.rowcount == 1


def delete_unbound_attachment(
    db: HubDatabase, attachment_id: str
) -> tuple[ChatAttachmentRecord, str] | None:
    """Claim an unbound attachment for standalone HTTP DELETE."""
    from gobby.storage.chat_attachment_lease import claim_attachment, new_claim_token

    token = new_claim_token()
    with db.transaction_immediate(ChatAttachmentMutation()) as conn:
        record = _fetch_attachment(conn, attachment_id)
        if record is None:
            return None
        if record.is_bound:
            raise ValueError("Only unbound queued attachments can be deleted")
        claimed = claim_attachment(
            conn,
            attachment_id=record.id,
            project_id=record.project_id,
            token=token,
            operation="http_delete",
        )
        if not claimed:
            raise ValueError("Only unbound queued attachments can be deleted")
        claimed_record = _fetch_attachment(conn, record.id, record.project_id)
    if claimed_record is None:
        return None
    return claimed_record, token


def list_stale_cleanup_candidates(
    db: HubDatabase,
    *,
    cutoff: datetime | str,
    limit: int = 500,
) -> list[ChatAttachmentRecord]:
    """Return unpublished-expired or expired-unbound rows for owner cleanup."""
    cutoff_value = parse_stored_datetime(cutoff)
    if cutoff_value is None:
        raise ValueError("cutoff is required")
    bounded_limit = max(1, int(limit))
    rows = db.fetchall(
        f"""
        SELECT {_ATTACHMENT_COLUMNS}
          FROM chat_attachments
         WHERE (
                published IS FALSE
                AND claimed_at < %s
               )
            OR (
                published IS TRUE
                AND conversation_id IS NULL
                AND message_id IS NULL
                AND target_session_id IS NULL
                AND bound_at IS NULL
                AND created_at < %s
                AND (claim_token IS NULL OR claimed_at < %s)
               )
         ORDER BY created_at ASC
         LIMIT %s
        """,
        (cutoff_value, cutoff_value, cutoff_value, bounded_limit),
    )
    return [_row_to_record(row) for row in rows]


def delete_stale_unbound_attachments(
    db: HubDatabase,
    *,
    cutoff: datetime | str,
    limit: int = 500,
) -> list[ChatAttachmentRecord]:
    """Compatibility alias used by hygiene until it claims through the lease."""
    return list_stale_cleanup_candidates(db, cutoff=cutoff, limit=limit)


def list_attachments_for_conversations(
    db: HubDatabase,
    conversation_ids: list[str],
    *,
    target_session_id: str | None = None,
) -> list[ChatAttachmentRecord]:
    """Return attachment rows bound to conversations, messages, or a session."""
    unique_ids = [value for value in dict.fromkeys(conversation_ids) if value]
    if not unique_ids and not target_session_id:
        return []

    clauses: list[str] = []
    params: list[object] = []
    if unique_ids:
        placeholders = ",".join("%s" for _ in unique_ids)
        clauses.append(
            f"""
            conversation_id IN ({placeholders})
            OR message_id IN (
                SELECT id::text
                  FROM chat_messages
                 WHERE conversation_id IN ({placeholders})
            )
            """
        )
        params.extend(unique_ids)
        params.extend(unique_ids)
    if target_session_id:
        clauses.append("target_session_id = %s")
        params.append(target_session_id)
    where = " OR ".join(f"({clause})" for clause in clauses)
    rows = db.fetchall(
        f"""
        SELECT {_ATTACHMENT_COLUMNS}
          FROM chat_attachments
         WHERE {where}
        """,  # nosec B608 # placeholders are generated from the conversation ID count only.
        tuple(params),
    )
    return [_row_to_record(row) for row in rows]


def delete_attachments_for_conversations(
    db: HubDatabase,
    conversation_ids: list[str],
) -> list[ChatAttachmentRecord]:
    """Select conversation-bound rows; callers claim and unlink through the lease."""
    return list_attachments_for_conversations(db, conversation_ids)
