"""Tests for WebSocket chat attachment binding helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import gobby.storage.chat_attachments as chat_attachments
from gobby.config.app import DaemonConfig
from gobby.servers.websocket.chat_attachments import (
    append_prepared_attachment_context,
    legacy_attachment_items,
    prepare_message_attachments,
)
from gobby.storage.config_store import ConfigStore
from gobby.storage.database import LocalDatabase
from gobby.storage.projects import PERSONAL_PROJECT_ID

pytestmark = pytest.mark.unit


def _owner(temp_db: LocalDatabase) -> SimpleNamespace:
    return SimpleNamespace(
        session_manager=SimpleNamespace(db=temp_db), daemon_config=DaemonConfig()
    )


def _attachment(
    temp_db: LocalDatabase,
    tmp_path: Path,
    *,
    attachment_id: str = "att-1",
    size_bytes: int = 5,
) -> str:
    """Create a stored attachment row backed by a temporary local file."""
    path = tmp_path / f"{attachment_id}.txt"
    path.write_bytes(b"x" * size_bytes)
    chat_attachments.create_attachment(
        temp_db,
        attachment_id=attachment_id,
        project_id=PERSONAL_PROJECT_ID,
        draft_id=None,
        filename=path.name,
        mime_type="text/plain",
        size_bytes=size_bytes,
        local_path=str(path),
    )
    return attachment_id


@pytest.mark.asyncio
async def test_prepare_message_attachments_binds_ids_and_formats_path_context(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    attachment_id = _attachment(temp_db, tmp_path)

    prepared = await prepare_message_attachments(
        _owner(temp_db),
        [{"id": attachment_id}],
        conversation_id="conv-1",
        message_id="msg-1",
    )

    assert prepared.records[0].id == attachment_id
    assert prepared.content_blocks[0]["attachment"]["id"] == attachment_id
    assert "att-1.txt" in (prepared.prompt_context or "")
    assert "base64" not in (prepared.prompt_context or "").lower()

    row = temp_db.fetchone("SELECT * FROM chat_attachments WHERE id = ?", (attachment_id,))
    assert row is not None
    assert row["conversation_id"] == "conv-1"
    assert row["message_id"] == "msg-1"


@pytest.mark.asyncio
async def test_prepare_message_attachments_honors_config_store_file_count(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    ConfigStore(temp_db).set("chat.attachment_max_files_per_message", 1)
    first = _attachment(temp_db, tmp_path, attachment_id="att-1")
    second = _attachment(temp_db, tmp_path, attachment_id="att-2")

    with pytest.raises(ValueError, match="Too many attachments"):
        await prepare_message_attachments(_owner(temp_db), [{"id": first}, {"id": second}])


@pytest.mark.asyncio
async def test_prepare_message_attachments_checks_current_file_size_limit_before_binding(
    temp_db: LocalDatabase,
    tmp_path: Path,
) -> None:
    ConfigStore(temp_db).set("chat.attachment_max_file_bytes", 4)
    attachment_id = _attachment(temp_db, tmp_path, size_bytes=5)

    with pytest.raises(ValueError, match="exceeds configured 4 byte limit"):
        await prepare_message_attachments(
            _owner(temp_db),
            [{"id": attachment_id}],
            conversation_id="conv-1",
        )

    row = temp_db.fetchone(
        "SELECT conversation_id FROM chat_attachments WHERE id = ?", (attachment_id,)
    )
    assert row is not None
    assert row["conversation_id"] is None


def test_legacy_attachment_items_filters_id_references() -> None:
    assert legacy_attachment_items(
        [
            {"id": "stored"},
            {"name": "legacy.txt", "base64": "aGVsbG8="},
        ]
    ) == [{"name": "legacy.txt", "base64": "aGVsbG8="}]


def test_append_prepared_attachment_context() -> None:
    prepared = SimpleNamespace(prompt_context="Attached files:\n- /tmp/a.txt")

    assert append_prepared_attachment_context("Look", prepared) == (
        "Look\n\nAttached files:\n- /tmp/a.txt"
    )
