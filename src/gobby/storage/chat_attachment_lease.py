"""Tokenized deletion and upload leases for chat attachments."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Literal

from gobby.storage.hub.protocol import ChatAttachmentMutation, HubDatabase, Transaction
from gobby.utils.datetime import utc_now

DELETION_LEASE_SECONDS = 60
UPLOAD_LEASE_SECONDS = 15 * 60

LeaseOperation = Literal["http_delete", "conversation", "clear_chat", "stale_upload"]


def new_claim_token() -> str:
    return str(uuid.uuid4())


def _expired_cutoff(*, seconds: int) -> datetime:
    return utc_now() - timedelta(seconds=seconds)


def claim_attachment(
    conn: Transaction,
    *,
    attachment_id: str,
    project_id: str,
    token: str,
    operation: LeaseOperation,
    conversation_id: str | None = None,
    target_session_id: str | None = None,
) -> bool:
    """CAS-claim a row for an owner delete or stale-upload cleanup."""
    now = utc_now()
    expired = _expired_cutoff(seconds=DELETION_LEASE_SECONDS)
    upload_expired = _expired_cutoff(seconds=UPLOAD_LEASE_SECONDS)
    if operation == "http_delete":
        cursor = conn.execute(
            """
            UPDATE chat_attachments
               SET claim_token = %s,
                   claimed_at = %s,
                   updated_at = %s
             WHERE id = %s
               AND project_id = %s
               AND conversation_id IS NULL
               AND message_id IS NULL
               AND target_session_id IS NULL
               AND (
                    claim_token IS NULL
                    OR claimed_at < %s
               )
            """,
            (token, now, now, attachment_id, project_id, expired),
        )
    elif operation == "conversation":
        cursor = conn.execute(
            """
            UPDATE chat_attachments
               SET claim_token = %s,
                   claimed_at = %s,
                   updated_at = %s
             WHERE id = %s
               AND project_id = %s
               AND (
                    conversation_id = %s
                    OR message_id IN (
                        SELECT id::text FROM chat_messages WHERE conversation_id = %s
                    )
               )
               AND (
                    claim_token IS NULL
                    OR claimed_at < %s
               )
            """,
            (
                token,
                now,
                now,
                attachment_id,
                project_id,
                conversation_id,
                conversation_id,
                expired,
            ),
        )
    elif operation == "clear_chat":
        cursor = conn.execute(
            """
            UPDATE chat_attachments
               SET claim_token = %s,
                   claimed_at = %s,
                   updated_at = %s
             WHERE id = %s
               AND project_id = %s
               AND (
                    conversation_id = %s
                    OR target_session_id = %s
                    OR message_id IN (
                        SELECT id::text FROM chat_messages WHERE conversation_id = %s
                    )
               )
               AND (
                    claim_token IS NULL
                    OR claimed_at < %s
               )
            """,
            (
                token,
                now,
                now,
                attachment_id,
                project_id,
                conversation_id,
                target_session_id,
                conversation_id,
                expired,
            ),
        )
    else:
        cursor = conn.execute(
            """
            UPDATE chat_attachments
               SET claim_token = %s,
                   claimed_at = %s,
                   updated_at = %s
             WHERE id = %s
               AND project_id = %s
               AND (
                    (
                        published IS FALSE
                        AND claimed_at < %s
                    )
                    OR (
                        published IS TRUE
                        AND conversation_id IS NULL
                        AND message_id IS NULL
                        AND target_session_id IS NULL
                        AND bound_at IS NULL
                        AND (claim_token IS NULL OR claimed_at < %s)
                    )
               )
            """,
            (
                token,
                now,
                now,
                attachment_id,
                project_id,
                upload_expired,
                expired,
            ),
        )
    return cursor.rowcount == 1


def renew_claim(conn: Transaction, *, attachment_id: str, project_id: str, token: str) -> bool:
    now = utc_now()
    cursor = conn.execute(
        """
        UPDATE chat_attachments
           SET claimed_at = %s,
               updated_at = %s
         WHERE id = %s
           AND project_id = %s
           AND claim_token = %s
        """,
        (now, now, attachment_id, project_id, token),
    )
    return cursor.rowcount == 1


def release_claim(conn: Transaction, *, attachment_id: str, project_id: str, token: str) -> bool:
    now = utc_now()
    cursor = conn.execute(
        """
        UPDATE chat_attachments
           SET claim_token = NULL,
               claimed_at = NULL,
               updated_at = %s
         WHERE id = %s
           AND project_id = %s
           AND claim_token = %s
        """,
        (now, attachment_id, project_id, token),
    )
    return cursor.rowcount == 1


def delete_claimed_row(
    conn: Transaction, *, attachment_id: str, project_id: str, token: str
) -> bool:
    cursor = conn.execute(
        """
        DELETE FROM chat_attachments
         WHERE id = %s
           AND project_id = %s
           AND claim_token = %s
        """,
        (attachment_id, project_id, token),
    )
    return cursor.rowcount == 1


def mark_published(conn: Transaction, *, attachment_id: str, project_id: str, token: str) -> bool:
    now = utc_now()
    cursor = conn.execute(
        """
        UPDATE chat_attachments
           SET published = TRUE,
               claim_token = NULL,
               claimed_at = NULL,
               updated_at = %s
         WHERE id = %s
           AND project_id = %s
           AND published IS FALSE
           AND claim_token = %s
        """,
        (now, attachment_id, project_id, token),
    )
    return cursor.rowcount == 1


def mark_published_db(db: HubDatabase, *, attachment_id: str, project_id: str, token: str) -> bool:
    with db.transaction_immediate(ChatAttachmentMutation()) as conn:
        return mark_published(conn, attachment_id=attachment_id, project_id=project_id, token=token)


def release_claim_db(db: HubDatabase, *, attachment_id: str, project_id: str, token: str) -> bool:
    with db.transaction_immediate(ChatAttachmentMutation()) as conn:
        return release_claim(conn, attachment_id=attachment_id, project_id=project_id, token=token)


def delete_claimed_row_db(
    db: HubDatabase, *, attachment_id: str, project_id: str, token: str
) -> bool:
    with db.transaction_immediate(ChatAttachmentMutation()) as conn:
        return delete_claimed_row(
            conn, attachment_id=attachment_id, project_id=project_id, token=token
        )


def renew_claim_db(db: HubDatabase, *, attachment_id: str, project_id: str, token: str) -> bool:
    with db.transaction_immediate(ChatAttachmentMutation()) as conn:
        return renew_claim(conn, attachment_id=attachment_id, project_id=project_id, token=token)
