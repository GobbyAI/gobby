"""Documents attached to inter-session messages.

One attachment belongs to one message, keyed by the message id, so retention
and session cascades delete it with its message. Readers are the message's
sender and recipient; every lookup filters on that.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from gobby.storage.hub.protocol import HubDatabase

MAX_ATTACHMENT_BYTES = 256 * 1024
MAX_FILENAME_LENGTH = 255
MEDIA_TYPES = ("text/plain", "text/markdown")
_UNSAFE_FILENAME_CHARS = re.compile(r"[/\\\x00-\x1f\x7f]")


class AttachmentError(ValueError):
    """A refused attachment. ``code`` is the typed error returned to callers."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class AttachmentDraft:
    """A validated document ready to store with its message."""

    filename: str
    media_type: str
    content: str
    size_bytes: int
    sha256: str

    @classmethod
    def build(cls, *, filename: str, media_type: str, content: str) -> AttachmentDraft:
        if (
            not filename.strip()
            or filename in (".", "..")
            or len(filename) > MAX_FILENAME_LENGTH
            or _UNSAFE_FILENAME_CHARS.search(filename)
        ):
            raise AttachmentError(
                "attachment_invalid_filename",
                f"filename must be a plain name of at most {MAX_FILENAME_LENGTH} characters "
                "with no path separators or control characters.",
            )
        if media_type not in MEDIA_TYPES:
            raise AttachmentError(
                "attachment_invalid_media_type",
                f"media_type must be one of: {', '.join(MEDIA_TYPES)}.",
            )
        if not content.strip():
            raise AttachmentError("attachment_empty", "document is required.")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_ATTACHMENT_BYTES:
            raise AttachmentError(
                "attachment_too_large",
                f"document is {len(encoded)} bytes; the limit is {MAX_ATTACHMENT_BYTES} bytes.",
            )
        return cls(
            filename=filename,
            media_type=media_type,
            content=content,
            size_bytes=len(encoded),
            sha256=hashlib.sha256(encoded).hexdigest(),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class StoredAttachment:
    """An attachment as read back from the database."""

    message_id: str
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    content: str

    def summary(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"message_id": self.message_id, **self.summary(), "content": self.content}


class InterSessionAttachmentManager:
    """Stores attachments and reads them for the message's sender or recipient."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def create(self, message_id: str, draft: AttachmentDraft) -> None:
        self.db.execute(
            """
            INSERT INTO inter_session_attachments
            (message_id, filename, media_type, content, size_bytes, sha256)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                message_id,
                draft.filename,
                draft.media_type,
                draft.content,
                draft.size_bytes,
                draft.sha256,
            ),
        )

    def get_for_caller(self, message_id: str, caller_session_id: str) -> StoredAttachment | None:
        """Return the attachment only when the caller sent or received its message.

        Missing, malformed, and not-yours all return None, so callers cannot tell them apart.
        """
        if not _is_uuid(message_id) or not _is_uuid(caller_session_id):
            return None
        row = self.db.fetchone(
            """
            SELECT a.message_id, a.filename, a.media_type, a.size_bytes, a.sha256, a.content
            FROM inter_session_attachments a
            JOIN inter_session_messages m ON m.id = a.message_id
            WHERE a.message_id = %s AND %s IN (m.from_session, m.to_session)
            """,
            (message_id, caller_session_id),
        )
        if row is None:
            return None
        return StoredAttachment(
            message_id=str(row["message_id"]),
            filename=row["filename"],
            media_type=row["media_type"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            content=row["content"],
        )

    def get_summary(self, message_id: str) -> dict[str, Any] | None:
        """Return the filename, type, size and digest. The caller must already be authorized."""
        if not _is_uuid(message_id):
            return None
        row = self.db.fetchone(
            """
            SELECT filename, media_type, size_bytes, sha256
            FROM inter_session_attachments
            WHERE message_id = %s
            """,
            (message_id,),
        )
        if row is None:
            return None
        return {
            "filename": row["filename"],
            "media_type": row["media_type"],
            "size_bytes": row["size_bytes"],
            "sha256": row["sha256"],
        }
