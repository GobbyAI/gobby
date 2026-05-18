"""WebSocket helpers for stored chat attachment references."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import gobby.storage.chat_attachments as chat_attachments
from gobby.servers.chat_attachment_limits import resolve_chat_attachment_limits
from gobby.servers.websocket.db import run_db
from gobby.storage.chat_attachments import ChatAttachmentRecord
from gobby.storage.config_store import ConfigStore
from gobby.storage.database import DatabaseProtocol


class AttachmentSessionManager(Protocol):
    @property
    def db(self) -> DatabaseProtocol | None: ...


class ChatAttachmentConfig(Protocol):
    attachment_max_file_bytes: int
    attachment_max_files_per_message: int
    attachment_max_total_bytes_per_message: int


class AttachmentDaemonConfig(Protocol):
    chat: ChatAttachmentConfig


class AttachmentOwner(Protocol):
    session_manager: AttachmentSessionManager | None
    daemon_config: AttachmentDaemonConfig | None


@dataclass(frozen=True)
class PreparedMessageAttachments:
    records: list[ChatAttachmentRecord]

    @property
    def content_blocks(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "attachment",
                "attachment": chat_attachments.to_api_dict(record),
            }
            for record in self.records
        ]

    @property
    def prompt_context(self) -> str | None:
        if not self.records:
            return None
        lines = [
            f"- {record.filename} ({record.mime_type}, {record.size_bytes} bytes): "
            f"{record.local_path or '<no local path>'}"
            for record in self.records
        ]
        return "Attached files are available on the local filesystem:\n" + "\n".join(lines)


def extract_attachment_ids(attachments: Any) -> list[str]:
    if not isinstance(attachments, list):
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for item in attachments:
        if not isinstance(item, dict):
            continue
        attachment_id = item.get("id")
        if isinstance(attachment_id, str) and attachment_id.strip():
            normalized = attachment_id.strip()
            if normalized not in seen:
                seen.add(normalized)
                ids.append(normalized)
    return ids


def legacy_attachment_items(attachments: Any) -> list[Any]:
    if not isinstance(attachments, list):
        return []
    return [
        item
        for item in attachments
        if not (isinstance(item, dict) and isinstance(item.get("id"), str))
    ]


def append_prepared_attachment_context(content: str, prepared: PreparedMessageAttachments) -> str:
    context = prepared.prompt_context
    if not context:
        return content
    if content.strip():
        return f"{content.rstrip()}\n\n{context}"
    return context


def _attachment_db(owner: AttachmentOwner) -> DatabaseProtocol:
    session_manager = getattr(owner, "session_manager", None)
    if session_manager is None:
        raise ValueError("Attachment storage requires session_manager")
    db = session_manager.db
    if db is None:
        raise ValueError("Attachment storage requires session_manager.db")
    return db


def _resolve_limits_sync(owner: AttachmentOwner) -> tuple[int, int, int]:
    db = _attachment_db(owner)
    limits = resolve_chat_attachment_limits(
        config_store=ConfigStore(db),
        daemon_config=getattr(owner, "daemon_config", None),
    )
    return (
        limits.max_file_bytes,
        limits.max_files_per_message,
        limits.max_total_bytes_per_message,
    )


def _bind_attachments_sync(
    owner: AttachmentOwner,
    attachment_ids: list[str],
    *,
    max_file_bytes: int,
    max_files_per_message: int,
    max_total_bytes: int,
    conversation_id: str | None,
    message_id: str | None,
    target_session_id: str | None,
) -> list[ChatAttachmentRecord]:
    db = _attachment_db(owner)
    if len(attachment_ids) > max_files_per_message:
        raise ValueError(f"Too many attachments: maximum is {max_files_per_message}")
    records = chat_attachments.get_attachments_by_ids(db, attachment_ids)
    found_ids = {record.id for record in records}
    for attachment_id in attachment_ids:
        if attachment_id not in found_ids:
            raise ValueError(f"Unknown attachment id: {attachment_id}")
    for record in records:
        if record.size_bytes > max_file_bytes:
            raise ValueError(
                f"Attachment {record.filename!r} exceeds configured {max_file_bytes} byte limit"
            )
    total_size = sum(record.size_bytes for record in records)
    if total_size > max_total_bytes:
        raise ValueError(f"Attachments exceed configured {max_total_bytes} byte total limit")
    return chat_attachments.bind_attachments(
        db,
        attachment_ids,
        conversation_id=conversation_id,
        message_id=message_id,
        target_session_id=target_session_id,
    )


async def prepare_message_attachments(
    owner: AttachmentOwner,
    attachments: Any,
    *,
    conversation_id: str | None = None,
    message_id: str | None = None,
    target_session_id: str | None = None,
) -> PreparedMessageAttachments:
    attachment_ids = extract_attachment_ids(attachments)
    if not attachment_ids:
        return PreparedMessageAttachments(records=[])

    max_file_bytes, max_files_per_message, max_total_bytes = await run_db(
        owner, _resolve_limits_sync, owner
    )
    if len(attachment_ids) > max_files_per_message:
        raise ValueError(f"Too many attachments: maximum is {max_files_per_message}")

    records = await run_db(
        owner,
        _bind_attachments_sync,
        owner,
        attachment_ids,
        max_file_bytes=max_file_bytes,
        max_files_per_message=max_files_per_message,
        max_total_bytes=max_total_bytes,
        conversation_id=conversation_id,
        message_id=message_id,
        target_session_id=target_session_id,
    )
    return PreparedMessageAttachments(records=records)
