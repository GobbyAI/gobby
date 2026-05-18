"""Tests for chat attachment metadata storage helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

import gobby.storage.chat_attachments as chat_attachments
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
    statements: list[str] = []

    temp_db.connection.set_trace_callback(statements.append)
    try:
        records = chat_attachments.bind_attachments(
            temp_db,
            [attachment_id],
            conversation_id="conv-1",
            message_id="msg-1",
            target_session_id=session.id,
        )
    finally:
        temp_db.connection.set_trace_callback(None)

    begin_index = next(
        i for i, statement in enumerate(statements) if statement == "BEGIN IMMEDIATE"
    )
    select_index = next(
        i for i, statement in enumerate(statements) if "FROM chat_attachments" in statement
    )
    update_index = next(
        i for i, statement in enumerate(statements) if "UPDATE chat_attachments" in statement
    )
    commit_index = next(i for i, statement in enumerate(statements) if statement == "COMMIT")
    assert begin_index < select_index < update_index < commit_index
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

    with pytest.raises(ValueError, match=f"Attachment {attachment_id} is already bound"):
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
    statements: list[str] = []
    temp_db.connection.set_trace_callback(statements.append)
    try:
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
    finally:
        temp_db.connection.set_trace_callback(None)

    begin_index = next(i for i, statement in enumerate(statements) if statement == "BEGIN")
    insert_index = next(
        i for i, statement in enumerate(statements) if "INSERT INTO chat_attachments" in statement
    )
    select_index = next(
        i for i, statement in enumerate(statements) if "FROM chat_attachments" in statement
    )
    commit_index = next(i for i, statement in enumerate(statements) if statement == "COMMIT")
    assert record.id == "attachment-1"
    assert begin_index < insert_index < select_index < commit_index


def test_delete_unbound_attachment_uses_immediate_transaction(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    attachment_id = _create_attachment(temp_db, tmp_path)
    statements: list[str] = []

    temp_db.connection.set_trace_callback(statements.append)
    try:
        record = chat_attachments.delete_unbound_attachment(temp_db, attachment_id)
    finally:
        temp_db.connection.set_trace_callback(None)

    assert record is not None
    assert record.id == attachment_id
    assert any(statement == "BEGIN IMMEDIATE" for statement in statements)
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
