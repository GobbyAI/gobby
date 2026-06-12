from unittest.mock import AsyncMock, MagicMock, patch

import aioimaplib
import pytest

from gobby.communications.adapters.email import EmailAdapter
from gobby.communications.models import ChannelConfig, CommsMessage


def _imap_response(result: str, lines: list[bytes]) -> aioimaplib.Response:
    return aioimaplib.Response(result, lines)


@pytest.fixture
def adapter():
    return EmailAdapter()


@pytest.fixture
def mock_secret_resolver():
    def _resolve(secret_ref: str) -> str | None:
        if secret_ref == "$secret:EMAIL_PASSWORD":
            return "pass123"
        return None

    return _resolve


@pytest.mark.asyncio
async def test_initialize(adapter, mock_secret_resolver):
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        config_json={
            "smtp_host": "smtp.example.com",
            "imap_host": "imap.example.com",
            "from_address": "bot@example.com",
        },
    )

    with patch("aiosmtplib.SMTP") as MockSMTP, patch("aioimaplib.IMAP4_SSL") as MockIMAP:
        mock_smtp_inst = AsyncMock()
        MockSMTP.return_value = mock_smtp_inst

        mock_imap_inst = AsyncMock()
        MockIMAP.return_value = mock_imap_inst

        await adapter.initialize(config, mock_secret_resolver)

        assert adapter._password == "pass123"
        assert adapter._smtp_client is mock_smtp_inst
        assert adapter._imap_client is mock_imap_inst

        mock_smtp_inst.connect.assert_called_once()
        mock_smtp_inst.login.assert_called_once_with("bot@example.com", "pass123")

        mock_imap_inst.wait_hello_from_server.assert_called_once()
        mock_imap_inst.login.assert_called_once_with("bot@example.com", "pass123")


@pytest.mark.asyncio
async def test_send_message(adapter, mock_secret_resolver):
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        config_json={
            "from_address": "bot@example.com",
            "smtp_host": "smtp.example.com",
            "to_address": "user@example.com",
        },
    )

    with patch("aiosmtplib.SMTP") as MockSMTP, patch("aioimaplib.IMAP4_SSL"):
        mock_smtp_inst = AsyncMock()
        MockSMTP.return_value = mock_smtp_inst
        await adapter.initialize(config, mock_secret_resolver)

    msg = CommsMessage(
        id="test_id",
        channel_id="gobby-internal-channel",
        direction="outbound",
        content="Hello via email",
        metadata_json={"platform_destination": "user@example.com", "subject": "Test Subject"},
        created_at="2024-01-01T00:00:00Z",
    )

    msg_id = await adapter.send_message(msg)

    assert msg.channel_id != msg.metadata_json["platform_destination"]
    assert msg_id is not None
    mock_smtp_inst.send_message.assert_called_once()

    sent_msg = mock_smtp_inst.send_message.call_args[0][0]
    assert sent_msg["Subject"] == "Test Subject"
    assert sent_msg["To"] == "user@example.com"
    assert sent_msg["From"] == "bot@example.com"


@pytest.mark.asyncio
async def test_poll(adapter, mock_secret_resolver):
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        config_json={"from_address": "bot@example.com", "imap_host": "imap.example.com"},
    )

    with patch("aiosmtplib.SMTP"), patch("aioimaplib.IMAP4_SSL") as MockIMAP:
        mock_imap_inst = AsyncMock()
        MockIMAP.return_value = mock_imap_inst
        await adapter.initialize(config, mock_secret_resolver)

        # Mock IMAP responses
        mock_imap_inst.search.return_value = _imap_response("OK", [b"1 2"])

        # Create a mock email payload
        email_content = b"From: user@example.com\r\nSubject: Test Reply\r\nMessage-ID: <msg123>\r\n\r\nHello back!"

        mock_imap_inst.fetch.side_effect = [
            _imap_response("OK", [b"1 (RFC822 {100})", email_content, b")"]),
            _imap_response("OK", [b"2 (RFC822 {100})", email_content, b")"]),
        ]
        mock_imap_inst.store.return_value = _imap_response("OK", [b""])

        messages = await adapter.poll()

        assert len(messages) == 2
        assert messages[0].channel_id == ""
        assert messages[0].metadata_json["platform_channel_id"] == "user@example.com"
        assert messages[0].content.strip() == "Hello back!"
        assert messages[0].platform_message_id == "<msg123>"
        assert messages[0].metadata_json["subject"] == "Test Reply"


@pytest.mark.asyncio
async def test_send_message_with_threading(adapter, mock_secret_resolver):
    """send_message() sets In-Reply-To and References headers when thread_id is present."""
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        config_json={
            "from_address": "bot@example.com",
            "smtp_host": "smtp.example.com",
            "to_address": "user@example.com",
        },
    )

    with patch("aiosmtplib.SMTP") as MockSMTP, patch("aioimaplib.IMAP4_SSL"):
        mock_smtp_inst = AsyncMock()
        MockSMTP.return_value = mock_smtp_inst
        await adapter.initialize(config, mock_secret_resolver)

    msg = CommsMessage(
        id="test_id",
        channel_id="gobby-internal-channel",
        direction="outbound",
        content="Thread reply",
        metadata_json={"platform_destination": "user@example.com", "subject": "Re: Original"},
        platform_thread_id="<original-msg-id@example.com>",
        created_at="2024-01-01T00:00:00Z",
    )

    msg_id = await adapter.send_message(msg)
    assert msg_id is not None

    sent_msg = mock_smtp_inst.send_message.call_args[0][0]
    assert sent_msg["In-Reply-To"] == "<original-msg-id@example.com>"
    assert sent_msg["References"] == "<original-msg-id@example.com>"


def test_capabilities_reports_threading(adapter):
    """Email adapter capabilities should report threading=True."""
    caps = adapter.capabilities()
    assert caps.threading is True
    assert caps.reactions is False


def test_parse_webhook(adapter):
    with pytest.raises(NotImplementedError):
        adapter.parse_webhook({}, {})


def test_verify_webhook(adapter):
    assert not adapter.verify_webhook(b"", {}, "")


@pytest.mark.asyncio
async def test_poll_marks_messages_as_seen(adapter, mock_secret_resolver):
    """poll() marks each fetched message with the \\Seen flag."""
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        config_json={"from_address": "bot@example.com", "imap_host": "imap.example.com"},
    )

    with patch("aiosmtplib.SMTP"), patch("aioimaplib.IMAP4_SSL") as MockIMAP:
        mock_imap_inst = AsyncMock()
        MockIMAP.return_value = mock_imap_inst
        await adapter.initialize(config, mock_secret_resolver)

        mock_imap_inst.search.return_value = _imap_response("OK", [b"1 2"])
        email_content = b"From: user@example.com\r\nSubject: Test\r\nMessage-ID: <m1>\r\n\r\nBody"
        mock_imap_inst.fetch.side_effect = [
            _imap_response("OK", [b"1 (RFC822 {50})", email_content, b")"]),
            _imap_response("OK", [b"2 (RFC822 {50})", email_content, b")"]),
        ]
        mock_imap_inst.store.return_value = _imap_response("OK", [b""])

        await adapter.poll()

        # Verify store was called for each message with \Seen flag
        assert mock_imap_inst.store.call_count == 2
        mock_imap_inst.store.assert_any_call("1", "+FLAGS", "(\\Seen)")
        mock_imap_inst.store.assert_any_call("2", "+FLAGS", "(\\Seen)")


@pytest.mark.asyncio
async def test_poll_does_not_mark_unparsed_fetch_as_seen(adapter, mock_secret_resolver):
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        config_json={"from_address": "bot@example.com", "imap_host": "imap.example.com"},
    )

    with patch("aiosmtplib.SMTP"), patch("aioimaplib.IMAP4_SSL") as MockIMAP:
        mock_imap_inst = AsyncMock()
        MockIMAP.return_value = mock_imap_inst
        await adapter.initialize(config, mock_secret_resolver)

        mock_imap_inst.search.return_value = _imap_response("OK", [b"1"])
        mock_imap_inst.fetch.return_value = _imap_response("OK", [b"1 (FLAGS (\\Recent))", b")"])

        messages = await adapter.poll()

        assert messages == []
        mock_imap_inst.store.assert_not_called()


@pytest.mark.asyncio
async def test_imap_oauth2_login_uses_aioimaplib_xoauth2(adapter):
    adapter._auth_method = "oauth2"
    adapter._from_address = "bot@example.com"
    adapter._oauth2_access_token = "access-token"
    adapter._oauth2_token_expiry = 9999999999

    class FakeIMAPClient:
        def __init__(self) -> None:
            self.xoauth2_calls: list[tuple[str, bytes]] = []

        async def xoauth2(self, user: str, token: bytes) -> aioimaplib.Response:
            self.xoauth2_calls.append((user, token))
            return _imap_response("OK", [b"Authenticated"])

    imap_client = FakeIMAPClient()

    await adapter._imap_login(imap_client)  # type: ignore[arg-type]

    assert imap_client.xoauth2_calls == [("bot@example.com", b"access-token")]


@pytest.mark.asyncio
async def test_imap_oauth2_login_rejects_non_ok_response(adapter):
    adapter._auth_method = "oauth2"
    adapter._from_address = "bot@example.com"
    adapter._oauth2_access_token = "access-token"
    adapter._oauth2_token_expiry = 9999999999

    class FakeIMAPClient:
        async def xoauth2(self, user: str, token: bytes) -> aioimaplib.Response:
            return _imap_response("NO", [b"Invalid credentials"])

    imap_client = FakeIMAPClient()

    with pytest.raises(ValueError, match="IMAP XOAUTH2 login failed: NO"):
        await adapter._imap_login(imap_client)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_smtp_reconnect_on_failure(adapter, mock_secret_resolver):
    """_ensure_smtp_connected() reconnects when NOOP fails."""
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        config_json={"from_address": "bot@example.com", "smtp_host": "smtp.example.com"},
    )

    with patch("aiosmtplib.SMTP") as MockSMTP, patch("aioimaplib.IMAP4_SSL"):
        mock_smtp_inst = AsyncMock()
        MockSMTP.return_value = mock_smtp_inst
        await adapter.initialize(config, mock_secret_resolver)

        # Simulate a dropped connection: is_connected returns False
        mock_smtp_inst.is_connected = False
        mock_smtp_inst.connect.reset_mock()
        mock_smtp_inst.login.reset_mock()

        await adapter._ensure_smtp_connected()

        # Should have reconnected
        mock_smtp_inst.connect.assert_called_once()
        assert mock_smtp_inst.connect.call_count == 1
        assert mock_smtp_inst.connect.call_args is not None
        mock_smtp_inst.login.assert_called_once_with("bot@example.com", "pass123")
        assert mock_smtp_inst.login.call_count == 1
        assert mock_smtp_inst.login.call_args is not None


@pytest.mark.asyncio
async def test_smtp_reconnect_on_noop_exception(adapter, mock_secret_resolver):
    """_ensure_smtp_connected() reconnects when NOOP raises an exception."""
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        config_json={"from_address": "bot@example.com", "smtp_host": "smtp.example.com"},
    )

    with patch("aiosmtplib.SMTP") as MockSMTP, patch("aioimaplib.IMAP4_SSL"):
        mock_smtp_inst = AsyncMock()
        MockSMTP.return_value = mock_smtp_inst
        await adapter.initialize(config, mock_secret_resolver)

        # Simulate NOOP failure (connection alive check fails)
        mock_smtp_inst.is_connected = True
        mock_smtp_inst.noop.side_effect = OSError("Connection reset")
        mock_smtp_inst.connect.reset_mock()
        mock_smtp_inst.login.reset_mock()

        await adapter._ensure_smtp_connected()

        mock_smtp_inst.connect.assert_called_once()
        assert mock_smtp_inst.connect.call_count == 1
        assert mock_smtp_inst.connect.call_args is not None
        mock_smtp_inst.login.assert_called_once_with("bot@example.com", "pass123")
        assert mock_smtp_inst.login.call_count == 1
        assert mock_smtp_inst.login.call_args is not None


@pytest.mark.asyncio
async def test_send_attachment_uses_async_file_read(adapter, mock_secret_resolver):
    """send_attachment() reads file bytes via asyncio.to_thread."""
    from gobby.communications.models import CommsAttachment

    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        config_json={
            "from_address": "bot@example.com",
            "smtp_host": "smtp.example.com",
            "to_address": "user@example.com",
        },
    )

    with patch("aiosmtplib.SMTP") as MockSMTP, patch("aioimaplib.IMAP4_SSL"):
        mock_smtp_inst = AsyncMock()
        MockSMTP.return_value = mock_smtp_inst
        await adapter.initialize(config, mock_secret_resolver)

    msg = CommsMessage(
        id="test_id",
        channel_id="user@example.com",
        direction="outbound",
        content="See attached",
        metadata_json={"subject": "File"},
        created_at="2024-01-01T00:00:00Z",
    )
    attachment = CommsAttachment(
        id="att_1",
        message_id="test_id",
        filename="test.txt",
        content_type="text/plain",
        size_bytes=12,
    )
    mock_path = MagicMock()
    mock_path.read_bytes.return_value = b"file content"

    with patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread:
        mock_to_thread.return_value = b"file content"
        await adapter.send_attachment(msg, attachment, mock_path)

        mock_to_thread.assert_called_once_with(mock_path.read_bytes)
        assert mock_to_thread.call_count == 1
        assert mock_to_thread.call_args is not None
