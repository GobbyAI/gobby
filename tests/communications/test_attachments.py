"""Tests for communications attachment manager and attachment flow."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.communications.attachments import PLATFORM_SIZE_LIMITS, AttachmentManager
from gobby.communications.models import CommsAttachment, CommsMessage
from gobby.storage.communications import LocalCommunicationsStore
from gobby.storage.database import LocalDatabase


@pytest.fixture
def attachment_dir(tmp_path: Path) -> Path:
    d = tmp_path / "comms_attachments"
    d.mkdir()
    return d


@pytest.fixture
def attachment_manager(attachment_dir: Path) -> AttachmentManager:
    return AttachmentManager(storage_dir=attachment_dir)


@pytest.mark.asyncio
async def test_store_async(attachment_manager: AttachmentManager) -> None:
    content = b"async content"
    path = await attachment_manager.store(content, "async_test.bin")
    assert path.exists()
    assert path.read_bytes() == content
    assert "async_test.bin" in path.name


@pytest.mark.asyncio
async def test_download(attachment_manager: AttachmentManager) -> None:
    mock_response = MagicMock()
    mock_response.content = b"downloaded data"
    mock_response.raise_for_status = MagicMock()

    with patch("gobby.communications.attachments.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        path = await attachment_manager.download("https://example.com/file.pdf", "file.pdf")

    assert path.exists()
    assert path.read_bytes() == b"downloaded data"
    assert "file.pdf" in path.name


@pytest.mark.asyncio
async def test_store_sanitizes_filename(attachment_manager: AttachmentManager) -> None:
    path = await attachment_manager.store(b"data", "../../../etc/passwd")
    assert "etc" not in str(path.parent.name)
    assert "passwd" in path.name


def test_get_path_nonexistent(attachment_manager: AttachmentManager) -> None:
    result = attachment_manager.get_path("nonexistent.txt")
    assert result is None


def test_cleanup_old(attachment_manager: AttachmentManager, attachment_dir: Path) -> None:
    old_file = attachment_dir / "old_file.txt"
    old_file.write_text("old")
    old_mtime = time.time() - (60 * 86400)
    os.utime(old_file, (old_mtime, old_mtime))

    new_file = attachment_dir / "new_file.txt"
    new_file.write_text("new")

    removed = attachment_manager.cleanup_old(days=30)
    assert removed == 1
    assert not old_file.exists()
    assert new_file.exists()


def test_validate_size(attachment_manager: AttachmentManager) -> None:
    assert attachment_manager.validate_size(1024, "telegram") is True
    assert attachment_manager.validate_size(100 * 1024 * 1024, "telegram") is False
    assert attachment_manager.validate_size(20 * 1024 * 1024, "unknown") is True
    assert attachment_manager.validate_size(30 * 1024 * 1024, "unknown") is False


def test_get_size_limit(attachment_manager: AttachmentManager) -> None:
    assert attachment_manager.get_size_limit("telegram") == PLATFORM_SIZE_LIMITS["telegram"]
    assert attachment_manager.get_size_limit("discord") == PLATFORM_SIZE_LIMITS["discord"]
    assert attachment_manager.get_size_limit("unknown") == 25 * 1024 * 1024


def test_storage_dir_created_automatically(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "attachments"
    mgr = AttachmentManager(storage_dir=target)
    assert target.exists()
    assert mgr.storage_dir == target


def test_comms_attachment_from_row() -> None:
    row = {
        "id": "ca-123",
        "message_id": "cm-456",
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "size_bytes": 12345,
        "local_path": "/tmp/report.pdf",
        "platform_url": "https://cdn.example.com/report.pdf",
        "created_at": "2024-01-01T00:00:00Z",
    }
    attachment = CommsAttachment.from_row(row)
    assert attachment.id == "ca-123"
    assert attachment.message_id == "cm-456"
    assert attachment.filename == "report.pdf"
    assert attachment.content_type == "application/pdf"
    assert attachment.size_bytes == 12345


def test_comms_attachment_from_row_defaults() -> None:
    row = {"id": "ca-789", "message_id": "cm-000", "filename": "file.bin"}
    attachment = CommsAttachment.from_row(row)
    assert attachment.content_type == "application/octet-stream"
    assert attachment.size_bytes == 0
    assert attachment.local_path is None
    assert attachment.platform_url is None


@pytest.fixture
def comms_store(temp_db: LocalDatabase) -> LocalCommunicationsStore:
    return LocalCommunicationsStore(temp_db, project_id="00000000-0000-0000-0000-000000000000")


def _create_test_channel(store: LocalCommunicationsStore) -> str:
    from gobby.communications.models import ChannelConfig

    channel = ChannelConfig(
        id="",
        channel_type="test",
        name=f"att-test-{time.time()}",
        enabled=True,
        config_json={},
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )
    return store.create_channel(channel).id


def _create_test_message(store: LocalCommunicationsStore, channel_id: str) -> str:
    msg = CommsMessage(
        id="",
        channel_id=channel_id,
        direction="outbound",
        content="test with attachment",
        content_type="attachment",
        created_at="2024-01-01T00:00:00Z",
    )
    return store.create_message(msg).id


def test_attachment_crud(comms_store: LocalCommunicationsStore) -> None:
    channel_id = _create_test_channel(comms_store)
    message_id = _create_test_message(comms_store, channel_id)

    attachment = CommsAttachment(
        id="",
        message_id=message_id,
        filename="test.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        local_path="/tmp/test.pdf",
        platform_url="https://cdn.example.com/test.pdf",
        created_at="2024-01-01T00:00:00Z",
    )
    saved = comms_store.create_attachment(attachment)
    assert saved.id.startswith("ca-")

    fetched = comms_store.get_attachment(saved.id)
    assert fetched is not None
    assert fetched.filename == "test.pdf"
    assert fetched.size_bytes == 1024

    attachments = comms_store.list_attachments(message_id)
    assert len(attachments) == 1

    comms_store.delete_attachment(saved.id)
    assert comms_store.get_attachment(saved.id) is None


def test_attachment_list_multiple(comms_store: LocalCommunicationsStore) -> None:
    channel_id = _create_test_channel(comms_store)
    message_id = _create_test_message(comms_store, channel_id)

    for i in range(3):
        comms_store.create_attachment(
            CommsAttachment(
                id="",
                message_id=message_id,
                filename=f"file_{i}.txt",
                content_type="text/plain",
                size_bytes=100 * (i + 1),
                created_at=f"2024-01-0{i + 1}T00:00:00Z",
            )
        )

    assert len(comms_store.list_attachments(message_id)) == 3


def test_delete_attachments_for_message(comms_store: LocalCommunicationsStore) -> None:
    channel_id = _create_test_channel(comms_store)
    message_id = _create_test_message(comms_store, channel_id)

    for i in range(3):
        comms_store.create_attachment(
            CommsAttachment(
                id="",
                message_id=message_id,
                filename=f"bulk_{i}.txt",
                content_type="text/plain",
                size_bytes=50,
                created_at="2024-01-01T00:00:00Z",
            )
        )

    assert comms_store.delete_attachments_for_message(message_id) == 3
    assert comms_store.list_attachments(message_id) == []


def test_attachment_cascade_on_message_delete(
    comms_store: LocalCommunicationsStore,
) -> None:
    channel_id = _create_test_channel(comms_store)
    message_id = _create_test_message(comms_store, channel_id)

    comms_store.create_attachment(
        CommsAttachment(
            id="",
            message_id=message_id,
            filename="cascade.txt",
            content_type="text/plain",
            size_bytes=10,
            created_at="2024-01-01T00:00:00Z",
        )
    )

    from datetime import datetime

    comms_store.delete_messages_before(datetime(2025, 1, 1))
    assert comms_store.list_attachments(message_id) == []


@pytest.mark.asyncio
async def test_telegram_send_attachment(tmp_path: Path) -> None:
    from gobby.communications.adapters.telegram import TelegramAdapter

    adapter = TelegramAdapter()
    adapter._api_base = "https://api.telegram.org/bot123"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.json.return_value = {"ok": True, "result": {"message_id": 42}}
    mock_response.raise_for_status = MagicMock()

    adapter._client = AsyncMock()
    adapter._client.post = AsyncMock(return_value=mock_response)

    file = tmp_path / "doc.pdf"
    file.write_bytes(b"pdf content")

    msg = CommsMessage(
        id="m1",
        channel_id="ch1",
        direction="outbound",
        content="Here is the file",
        created_at="2024-01-01",
        metadata_json={"chat_id": "12345"},
    )
    att = CommsAttachment(
        id="a1",
        message_id="m1",
        filename="doc.pdf",
        content_type="application/pdf",
        size_bytes=11,
    )

    result = await adapter.send_attachment(msg, att, file)
    assert result == "42"
    assert adapter._client.post.await_args.args[0].endswith("/sendDocument")


@pytest.mark.asyncio
async def test_slack_send_attachment(tmp_path: Path) -> None:
    from gobby.communications.adapters.slack import SlackAdapter

    adapter = SlackAdapter()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.json.return_value = {
        "ok": True,
        "upload_url": "https://files.slack.com/upload/v1/test",
        "file_id": "F_TEST",
    }
    mock_complete = MagicMock()
    mock_complete.status_code = 200
    mock_complete.headers = {}
    mock_complete.json.return_value = {
        "ok": True,
        "files": [{"shares": {"public": {"C123": [{"ts": "1234567890.123456"}]}}}],
    }
    mock_complete.raise_for_status = MagicMock()

    adapter._client = AsyncMock()
    adapter._client.post = AsyncMock(side_effect=[mock_response, mock_complete])

    file = tmp_path / "report.csv"
    file.write_bytes(b"a,b,c")

    msg = CommsMessage(
        id="m2",
        channel_id="C123",
        direction="outbound",
        content="Report attached",
        created_at="2024-01-01",
    )
    att = CommsAttachment(
        id="a2",
        message_id="m2",
        filename="report.csv",
        content_type="text/csv",
        size_bytes=5,
    )

    # Mock the httpx.AsyncClient used for the PUT upload in step 2
    mock_upload_client = AsyncMock()
    mock_upload_resp = MagicMock()
    mock_upload_resp.status_code = 200
    mock_upload_resp.headers = {}
    mock_upload_resp.raise_for_status = MagicMock()
    mock_upload_client.put = AsyncMock(return_value=mock_upload_resp)
    mock_upload_client.__aenter__ = AsyncMock(return_value=mock_upload_client)
    mock_upload_client.__aexit__ = AsyncMock(return_value=False)

    with patch(
        "gobby.communications.adapters.slack.httpx.AsyncClient", return_value=mock_upload_client
    ):
        result = await adapter.send_attachment(msg, att, file)
    assert result == "1234567890.123456"
    assert adapter._client.post.await_count == 2


@pytest.mark.asyncio
async def test_discord_send_attachment(tmp_path: Path) -> None:
    from gobby.communications.adapters.discord import DiscordAdapter

    adapter = DiscordAdapter()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.json.return_value = {"id": "999888777"}
    mock_response.raise_for_status = MagicMock()

    adapter._client = AsyncMock()
    adapter._client.post = AsyncMock(return_value=mock_response)

    file = tmp_path / "image.png"
    file.write_bytes(b"\x89PNG")

    msg = CommsMessage(
        id="m3",
        channel_id="discord-ch",
        direction="outbound",
        content="",
        created_at="2024-01-01",
    )
    att = CommsAttachment(
        id="a3",
        message_id="m3",
        filename="image.png",
        content_type="image/png",
        size_bytes=4,
    )

    result = await adapter.send_attachment(msg, att, file)
    assert result == "999888777"
    assert adapter._route_buckets == {}


@pytest.mark.asyncio
async def test_email_send_attachment(tmp_path: Path) -> None:
    from gobby.communications.adapters.email import EmailAdapter

    adapter = EmailAdapter()
    adapter._from_address = "bot@gobby.local"
    adapter._default_destination = "user@example.com"
    mock_smtp = AsyncMock()
    adapter._smtp_client = mock_smtp

    with patch("gobby.communications.adapters.email.HAS_SMTP", True):
        file = tmp_path / "data.json"
        file.write_bytes(b'{"key": "value"}')

        msg = CommsMessage(
            id="m4",
            channel_id="user@example.com",
            direction="outbound",
            content="See attached",
            created_at="2024-01-01",
            metadata_json={"subject": "Data file"},
        )
        att = CommsAttachment(
            id="a4",
            message_id="m4",
            filename="data.json",
            content_type="application/json",
            size_bytes=16,
        )

        result = await adapter.send_attachment(msg, att, file)
        assert result is not None
        mock_smtp.send_message.assert_called_once()


@pytest.mark.asyncio
async def test_sms_send_attachment_requires_url() -> None:
    from gobby.communications.adapters.sms import SMSAdapter

    adapter = SMSAdapter()
    adapter._client = AsyncMock()
    adapter._from_number = "+15551234567"

    msg = CommsMessage(
        id="m5",
        channel_id="+15559876543",
        direction="outbound",
        content="",
        created_at="2024-01-01",
    )
    att = CommsAttachment(
        id="a5",
        message_id="m5",
        filename="photo.jpg",
        content_type="image/jpeg",
        size_bytes=1000,
        platform_url=None,
    )

    with pytest.raises(ValueError, match="platform_url"):
        await adapter.send_attachment(msg, att, Path("/tmp/photo.jpg"))


@pytest.mark.asyncio
async def test_sms_send_attachment_with_url() -> None:
    from gobby.communications.adapters.sms import SMSAdapter

    adapter = SMSAdapter()
    adapter._from_number = "+15551234567"
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {}
    mock_response.json.return_value = {"sid": "SM123abc"}
    mock_response.raise_for_status = MagicMock()

    adapter._client = AsyncMock()
    adapter._client.post = AsyncMock(return_value=mock_response)

    msg = CommsMessage(
        id="m6",
        channel_id="+15559876543",
        direction="outbound",
        content="Check this out",
        created_at="2024-01-01",
    )
    att = CommsAttachment(
        id="a6",
        message_id="m6",
        filename="photo.jpg",
        content_type="image/jpeg",
        size_bytes=1000,
        platform_url="https://media.example.com/photo.jpg",
    )

    result = await adapter.send_attachment(msg, att, Path("/tmp/photo.jpg"))
    assert result == "SM123abc"
    assert adapter._client.post.await_args.kwargs["data"]["MediaUrl"] == (
        "https://media.example.com/photo.jpg"
    )


@pytest.mark.asyncio
async def test_base_adapter_send_attachment_raises() -> None:
    from gobby.communications.adapters.base import BaseChannelAdapter
    from gobby.communications.models import ChannelCapabilities

    class MinimalAdapter(BaseChannelAdapter):
        @property
        def channel_type(self) -> str:
            return "minimal"

        @property
        def max_message_length(self) -> int:
            return 1000

        @property
        def supports_webhooks(self) -> bool:
            return False

        @property
        def supports_polling(self) -> bool:
            return False

        async def initialize(self, config, secret_resolver):  # type: ignore[override]
            pass

        async def send_message(self, message):  # type: ignore[override]
            return None

        async def shutdown(self):
            pass

        def capabilities(self):
            return ChannelCapabilities()

        def parse_webhook(self, payload, headers):  # type: ignore[override]
            return []

        def verify_webhook(self, payload, headers, secret):  # type: ignore[override]
            return False

    adapter = MinimalAdapter()
    msg = CommsMessage(
        id="m",
        channel_id="ch",
        direction="outbound",
        content="",
        created_at="2024-01-01",
    )
    att = CommsAttachment(
        id="a",
        message_id="m",
        filename="f.txt",
        content_type="text/plain",
        size_bytes=0,
    )

    with pytest.raises(NotImplementedError, match="minimal"):
        await adapter.send_attachment(msg, att, Path("/tmp/f.txt"))
