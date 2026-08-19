"""Owner cleanup for conversation delete, clear-chat, and stale uploads."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from gobby.paths import FilesHomeError, FilesHomeNotOnThisDaemonError, get_files_home
from gobby.servers.chat_attachment_files import unlink_attachment_bytes
from gobby.servers.chat_attachment_workers import run_shielded
from gobby.storage import chat_attachments
from gobby.storage.chat_attachment_fence import (
    CleanupFenceConflict,
    acquire_cleanup_fence,
    finish_cleanup_fence,
)
from gobby.storage.chat_attachment_lease import (
    claim_attachment,
    delete_claimed_row,
    new_claim_token,
    release_claim,
    renew_claim,
)
from gobby.storage.hub.protocol import ChatAttachmentMutation, HubDatabase

RunDb = Callable[..., Awaitable[Any]]


def cleanup_conversation_attachments_sync(
    db: HubDatabase,
    conversation_id: str,
    *,
    target_session_id: str | None = None,
    terminal: bool,
    owner: str = "cleanup",
) -> list[chat_attachments.ChatAttachmentRecord]:
    """Claim, unlink, and delete rows for one conversation or session scope."""
    if get_files_home() is None:
        return []
    token = new_claim_token()
    removed: list[chat_attachments.ChatAttachmentRecord] = []
    with db.transaction_immediate(ChatAttachmentMutation()) as conn:
        try:
            acquire_cleanup_fence(
                conn,
                scope_kind="conversation",
                scope_id=conversation_id,
                token=token,
                owner=owner,
                reclaim_expired_clear_chat=not terminal,
            )
            if target_session_id:
                acquire_cleanup_fence(
                    conn,
                    scope_kind="session",
                    scope_id=target_session_id,
                    token=token,
                    owner=owner,
                    reclaim_expired_clear_chat=not terminal,
                )
        except CleanupFenceConflict:
            return []
    while True:
        records = chat_attachments.list_attachments_for_conversations(
            db, [conversation_id], target_session_id=target_session_id
        )
        if not records:
            break
        progressed = False
        for record in records:
            with db.transaction_immediate(ChatAttachmentMutation()) as conn:
                claimed = claim_attachment(
                    conn,
                    attachment_id=record.id,
                    project_id=record.project_id,
                    token=token,
                    operation="conversation" if terminal else "clear_chat",
                    conversation_id=conversation_id,
                    target_session_id=target_session_id,
                )
                if claimed:
                    renew_claim(
                        conn,
                        attachment_id=record.id,
                        project_id=record.project_id,
                        token=token,
                    )
            if not claimed:
                continue
            progressed = True
            try:
                unlink_attachment_bytes(record.project_id, record.id, record.filename)
            except FileNotFoundError:
                pass
            except FilesHomeError:
                with db.transaction_immediate(ChatAttachmentMutation()) as conn:
                    release_claim(
                        conn,
                        attachment_id=record.id,
                        project_id=record.project_id,
                        token=token,
                    )
                continue
            with db.transaction_immediate(ChatAttachmentMutation()) as conn:
                delete_claimed_row(
                    conn,
                    attachment_id=record.id,
                    project_id=record.project_id,
                    token=token,
                )
            removed.append(record)
        if not progressed:
            break
    with db.transaction_immediate(ChatAttachmentMutation()) as conn:
        finish_cleanup_fence(
            conn, scope_kind="conversation", scope_id=conversation_id, terminal=terminal
        )
        if target_session_id:
            finish_cleanup_fence(
                conn, scope_kind="session", scope_id=target_session_id, terminal=False
            )
    return removed


async def cleanup_conversation_attachments(
    db: HubDatabase,
    conversation_id: str,
    *,
    run_db: RunDb,
    target_session_id: str | None = None,
    terminal: bool,
) -> list[chat_attachments.ChatAttachmentRecord]:
    outcome, cancelled = await run_shielded(
        "attachment-conversation-cleanup",
        cleanup_conversation_attachments_sync,
        db,
        conversation_id,
        target_session_id=target_session_id,
        terminal=terminal,
    )
    del run_db
    if cancelled:
        raise asyncio.CancelledError
    if outcome.error is not None:
        raise outcome.error
    return outcome.result or []


def cleanup_stale_attachments_sync(
    db: HubDatabase,
    *,
    cutoff: datetime | str,
    limit: int,
) -> list[chat_attachments.ChatAttachmentRecord]:
    if get_files_home() is None:
        return []
    candidates = chat_attachments.list_stale_cleanup_candidates(db, cutoff=cutoff, limit=limit)
    removed: list[chat_attachments.ChatAttachmentRecord] = []
    for record in candidates:
        token = new_claim_token()
        with db.transaction_immediate(ChatAttachmentMutation()) as conn:
            claimed = claim_attachment(
                conn,
                attachment_id=record.id,
                project_id=record.project_id,
                token=token,
                operation="stale_upload",
            )
        if not claimed:
            continue
        try:
            unlink_attachment_bytes(record.project_id, record.id, record.filename)
        except FileNotFoundError:
            pass
        except (FilesHomeError, FilesHomeNotOnThisDaemonError):
            with db.transaction_immediate(ChatAttachmentMutation()) as conn:
                release_claim(
                    conn,
                    attachment_id=record.id,
                    project_id=record.project_id,
                    token=token,
                )
            continue
        with db.transaction_immediate(ChatAttachmentMutation()) as conn:
            delete_claimed_row(
                conn,
                attachment_id=record.id,
                project_id=record.project_id,
                token=token,
            )
        removed.append(record)
    return removed
