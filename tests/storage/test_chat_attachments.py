"""Tests for chat attachment metadata storage helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import gobby.storage.chat_attachments as chat_attachments
from gobby.storage import chat_messages
from gobby.storage.database import LocalDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


def _create_attachment(
    temp_db: LocalDatabase,
    tmp_path: Path,
    attachment_id: str = "attachment-1",
) -> str:
    path = tmp_path / f"{attachment_id}.txt"
    path.write_text("queued")
    chat_attachments.create_attachment(
        temp_db,
        attachment_id=attachment_id,
        project_id=PERSONAL_PROJECT_ID,
        draft_id=None,
        filename=path.name,
        mime_type="text/plain",
        size_bytes=path.stat().st_size,
        local_path=str(path),
    )
    return attachment_id


def test_bind_attachments_uses_immediate_transaction_for_read_validate_update(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    attachment_id = _create_attachment(temp_db, tmp_path)
    session = SessionManager(temp_db).register(
        external_id="cli-session",
        machine_id="machine",
        source="codex",
        project_id=PERSONAL_PROJECT_ID,
    )
    with patch.object(
        temp_db, "transaction_immediate", wraps=temp_db.transaction_immediate
    ) as transaction_immediate:
        records = chat_attachments.bind_attachments(
            temp_db,
            [attachment_id],
            conversation_id="conv-1",
            message_id="msg-1",
            target_session_id=session.id,
        )

    transaction_immediate.assert_called_once()
    assert isinstance(
        transaction_immediate.call_args.args[0],
        chat_attachments.ChatAttachmentMutation,
    )
    assert records[0].conversation_id == "conv-1"
    assert records[0].message_id == "msg-1"
    assert records[0].target_session_id == session.id
    assert records[0].bound_at is not None


def test_bind_attachments_is_idempotent_for_same_targets(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    attachment_id = _create_attachment(temp_db, tmp_path)
    first = chat_attachments.bind_attachments(
        temp_db,
        [attachment_id],
        conversation_id="conv-1",
        message_id="msg-1",
    )[0]

    second = chat_attachments.bind_attachments(
        temp_db,
        [attachment_id],
        conversation_id="conv-1",
        message_id="msg-1",
    )[0]

    assert second.conversation_id == "conv-1"
    assert second.message_id == "msg-1"
    assert second.bound_at == first.bound_at


def test_bind_attachments_allows_partial_binding_completion(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    attachment_id = _create_attachment(temp_db, tmp_path)
    chat_attachments.bind_attachments(temp_db, [attachment_id], conversation_id="conv-1")

    record = chat_attachments.bind_attachments(
        temp_db,
        [attachment_id],
        conversation_id="conv-1",
        message_id="msg-1",
    )[0]

    assert record.conversation_id == "conv-1"
    assert record.message_id == "msg-1"


def test_bind_attachments_rejects_conflicting_binding(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    attachment_id = _create_attachment(temp_db, tmp_path)
    chat_attachments.bind_attachments(temp_db, [attachment_id], conversation_id="conv-1")

    with pytest.raises(
        ValueError,
        match=f"Attachment {attachment_id} is already bound: conversation_id='conv-1'",
    ):
        chat_attachments.bind_attachments(temp_db, [attachment_id], conversation_id="conv-2")


def test_bind_attachments_preserves_bound_at_on_later_partial_updates(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    attachment_id = _create_attachment(temp_db, tmp_path)
    first = chat_attachments.bind_attachments(temp_db, [attachment_id], conversation_id="conv-1")[0]

    second = chat_attachments.bind_attachments(
        temp_db,
        [attachment_id],
        conversation_id="conv-1",
        message_id="msg-1",
    )[0]

    assert second.bound_at == first.bound_at
    assert second.updated_at >= first.updated_at


def test_create_attachment_fetches_created_row_inside_transaction(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    with patch.object(temp_db, "transaction", wraps=temp_db.transaction) as transaction:
        record = chat_attachments.create_attachment(
            temp_db,
            attachment_id="attachment-1",
            project_id=PERSONAL_PROJECT_ID,
            draft_id="draft-1",
            filename="attachment.txt",
            mime_type="text/plain",
            size_bytes=6,
            local_path=str(tmp_path / "attachment.txt"),
        )

    assert record.id == "attachment-1"
    transaction.assert_called_once()


def test_delete_unbound_attachment_uses_immediate_transaction(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    attachment_id = _create_attachment(temp_db, tmp_path)
    with patch.object(
        temp_db, "transaction_immediate", wraps=temp_db.transaction_immediate
    ) as transaction_immediate:
        record = chat_attachments.delete_unbound_attachment(temp_db, attachment_id)

    assert record is not None
    assert record.id == attachment_id
    transaction_immediate.assert_called_once()
    assert isinstance(
        transaction_immediate.call_args.args[0],
        chat_attachments.ChatAttachmentMutation,
    )
    assert chat_attachments.get_attachment(temp_db, attachment_id) is None


def test_delete_unbound_attachment_keeps_bound_attachment(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    attachment_id = _create_attachment(temp_db, tmp_path)
    chat_attachments.bind_attachments(
        temp_db,
        [attachment_id],
        conversation_id="conv-1",
        message_id="msg-1",
    )

    with pytest.raises(ValueError, match="Only unbound queued attachments can be deleted"):
        chat_attachments.delete_unbound_attachment(temp_db, attachment_id)

    assert chat_attachments.get_attachment(temp_db, attachment_id) is not None


def test_delete_stale_unbound_attachments_deletes_only_never_bound_old_rows(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    old_unbound = _create_attachment(temp_db, tmp_path, "old-unbound")
    fresh_unbound = _create_attachment(temp_db, tmp_path, "fresh-unbound")
    bound = _create_attachment(temp_db, tmp_path, "bound")
    historically_bound = _create_attachment(temp_db, tmp_path, "historically-bound")
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    old = cutoff - timedelta(minutes=1)
    fresh = cutoff + timedelta(minutes=1)

    chat_attachments.bind_attachments(temp_db, [bound], conversation_id="conv-1")
    temp_db.execute(
        """
        UPDATE chat_attachments
           SET created_at = ?, updated_at = ?
         WHERE id = ?
        """,
        (old, old, old_unbound),
    )
    temp_db.execute(
        """
        UPDATE chat_attachments
           SET created_at = ?, updated_at = ?
         WHERE id = ?
        """,
        (fresh, fresh, fresh_unbound),
    )
    temp_db.execute(
        """
        UPDATE chat_attachments
           SET created_at = ?, updated_at = ?
         WHERE id = ?
        """,
        (old, old, bound),
    )
    temp_db.execute(
        """
        UPDATE chat_attachments
           SET created_at = ?, updated_at = ?, bound_at = ?
         WHERE id = ?
        """,
        (old, old, old, historically_bound),
    )

    deleted = chat_attachments.delete_stale_unbound_attachments(temp_db, cutoff=cutoff)

    assert [record.id for record in deleted] == [old_unbound]
    assert chat_attachments.get_attachment(temp_db, old_unbound) is None
    assert chat_attachments.get_attachment(temp_db, fresh_unbound) is not None
    assert chat_attachments.get_attachment(temp_db, bound) is not None
    assert chat_attachments.get_attachment(temp_db, historically_bound) is not None


def test_delete_attachments_for_conversations_removes_conversation_and_message_links(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    conversation_attachment = _create_attachment(temp_db, tmp_path, "conversation-attachment")
    message_attachment = _create_attachment(temp_db, tmp_path, "message-attachment")
    other_attachment = _create_attachment(temp_db, tmp_path, "other-attachment")
    message_id = chat_messages.save_message(
        temp_db,
        conversation_id="conv-1",
        role="user",
        content="hello",
    )
    chat_attachments.bind_attachments(
        temp_db,
        [conversation_attachment],
        conversation_id="conv-1",
    )
    chat_attachments.bind_attachments(
        temp_db,
        [message_attachment],
        message_id=message_id,
    )
    chat_attachments.bind_attachments(
        temp_db,
        [other_attachment],
        conversation_id="conv-2",
    )

    deleted = chat_attachments.delete_attachments_for_conversations(temp_db, ["conv-1"])

    assert {record.id for record in deleted} == {conversation_attachment, message_attachment}
    assert chat_attachments.get_attachment(temp_db, conversation_attachment) is None
    assert chat_attachments.get_attachment(temp_db, message_attachment) is None
    assert chat_attachments.get_attachment(temp_db, other_attachment) is not None
