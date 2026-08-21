"""Tests for gobby.communications.adapters.email."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aioimaplib
import aiosmtplib
import pytest

from gobby.communications.adapters.email import EmailAdapter
from gobby.communications.models import ChannelConfig, CommsMessage

pytestmark = pytest.mark.unit

_TIMESTAMP = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
def adapter() -> EmailAdapter:
    return EmailAdapter()


@pytest.fixture
def config() -> MagicMock:
    mock_config = MagicMock()
    mock_config.config_json = {
        "smtp_host": "smtp.test.com",
        "smtp_port": 587,
        "imap_host": "imap.test.com",
        "imap_port": 993,
        "from_address": "bot@test.com",
        "to_address": "user@test.com",
        "password": "fake-password",
    }
    return mock_config


class TestEmailAdapter:
    @pytest.mark.asyncio
    @patch("gobby.communications.adapters.email.aiosmtplib", create=True)
    @patch("gobby.communications.adapters.email.aioimaplib", create=True)
    async def test_initialize_success(
        self, mock_imap: MagicMock, mock_smtp: MagicMock, adapter: EmailAdapter, config: MagicMock
    ) -> None:
        # Setup mocks
        mock_smtp_client = AsyncMock()
        mock_smtp_client.login.return_value = SimpleNamespace(code=235, message="OK")
        mock_smtp.SMTP.return_value = mock_smtp_client

        mock_imap_client = AsyncMock()
        mock_imap_client.login.return_value = SimpleNamespace(result="OK", lines=[])
        mock_imap.IMAP4_SSL.return_value = mock_imap_client

        # Mock secret resolver
        def resolver(ref: str) -> str:
            return "secret-pass"

        # Apply settings
        config.config_json["password"] = "$secret:EMAIL_PASSWORD"

        await adapter.initialize(config, resolver)

        assert adapter._smtp_host == "smtp.test.com"
        assert adapter._imap_host == "imap.test.com"
        assert adapter._from_address == "bot@test.com"
        assert adapter._password == "secret-pass"

        mock_smtp.SMTP.assert_called_with(
            hostname="smtp.test.com", port=587, use_tls=False, start_tls=True
        )
        mock_smtp_client.connect.assert_called_once()
        mock_smtp_client.login.assert_called_once_with("bot@test.com", "secret-pass")

        mock_imap.IMAP4_SSL.assert_called_with(host="imap.test.com", port=993)
        mock_imap_client.wait_hello_from_server.assert_called_once()
        mock_imap_client.login.assert_called_once_with("bot@test.com", "secret-pass")

    @pytest.mark.asyncio
    async def test_initialize_missing_password(
        self, adapter: EmailAdapter, config: MagicMock
    ) -> None:
        config.config_json["password"] = "$secret:MISSING"

        def resolver(ref: str) -> str | None:
            return None

        with pytest.raises(ValueError, match="Could not resolve Email password"):
            await adapter.initialize(config, resolver)

    @pytest.mark.asyncio
    @patch("gobby.communications.adapters.email.aiosmtplib", create=True)
    @patch("gobby.communications.adapters.email.aioimaplib", create=True)
    async def test_initialize_failure_shuts_down_created_clients(
        self, mock_imap: MagicMock, mock_smtp: MagicMock, adapter: EmailAdapter, config: MagicMock
    ) -> None:
        mock_smtp_client = AsyncMock()
        mock_smtp.SMTP.return_value = mock_smtp_client

        mock_imap_client = AsyncMock()
        mock_imap_client.wait_hello_from_server.side_effect = RuntimeError("imap failed")
        mock_imap.IMAP4_SSL.return_value = mock_imap_client

        def resolver(ref: str) -> str:
            return "secret-pass"

        config.config_json["password"] = "$secret:EMAIL_PASSWORD"

        with pytest.raises(RuntimeError, match="imap failed"):
            await adapter.initialize(config, resolver)

        mock_smtp_client.quit.assert_called_once()
        mock_imap_client.close.assert_called_once()
        mock_imap_client.logout.assert_called_once()
        assert adapter._smtp_client is None
        assert adapter._imap_client is None

    @pytest.mark.asyncio
    @patch("gobby.communications.adapters.email.aiosmtplib", create=True)
    async def test_ensure_smtp_connected_already_connected(
        self, mock_smtp: MagicMock, adapter: EmailAdapter
    ) -> None:
        adapter._smtp_client = AsyncMock()
        adapter._smtp_client.is_connected = True

        await adapter._ensure_smtp_connected()
        adapter._smtp_client.noop.assert_called_once()

    @pytest.mark.asyncio
    @patch("gobby.communications.adapters.email.aiosmtplib", create=True)
    async def test_ensure_smtp_connected_reconnects(
        self, mock_smtp: MagicMock, adapter: EmailAdapter
    ) -> None:
        old_client = AsyncMock()
        old_client.is_connected = False
        adapter._smtp_client = old_client
        adapter._smtp_host = "test"
        adapter._smtp_port = 587
        adapter._from_address = "bot"
        adapter._password = "pass"

        new_client = AsyncMock()
        mock_smtp.SMTP.return_value = new_client

        await adapter._ensure_smtp_connected()

        old_client.close.assert_called_once()
        mock_smtp.SMTP.assert_called_once()
        new_client.connect.assert_called_once()
        new_client.login.assert_called_with("bot", "pass")

    @pytest.mark.asyncio
    async def test_send_message_uninitialized(self, adapter: EmailAdapter) -> None:
        adapter._smtp_client = None
        msg = MagicMock()
        with pytest.raises(RuntimeError):
            await adapter.send_message(msg)

    @pytest.mark.asyncio
    @patch("gobby.communications.adapters.email.aiosmtplib", create=True)
    async def test_send_message_success(self, mock_smtp: MagicMock, adapter: EmailAdapter) -> None:
        adapter._smtp_client = AsyncMock()
        adapter._smtp_client.is_connected = True
        adapter._from_address = "bot@test.com"
        adapter._default_destination = "user@test.com"

        msg = MagicMock()
        msg.content = "hello world"
        msg.metadata_json = {"subject": "Test Subj"}
        msg.platform_thread_id = None
        msg.content_type = "text"

        msg_id = await adapter.send_message(msg)

        assert isinstance(msg_id, str) and len(msg_id) > 0
        adapter._smtp_client.send_message.assert_called_once()

        # Check email message was constructed correctly
        sent_email = adapter._smtp_client.send_message.call_args[0][0]
        assert isinstance(sent_email, EmailMessage)
        assert sent_email["Subject"] == "Test Subj"
        assert sent_email["To"] == "user@test.com"
        assert sent_email.get_content().strip() == "hello world"

    @pytest.mark.asyncio
    @patch("gobby.communications.adapters.email.aiosmtplib", create=True)
    async def test_send_message_with_html_and_reply(
        self, mock_smtp: MagicMock, adapter: EmailAdapter
    ) -> None:
        adapter._smtp_client = AsyncMock()
        adapter._smtp_client.is_connected = True
        adapter._from_address = "bot@test.com"
        adapter._default_destination = "user@test.com"

        msg = MagicMock()
        msg.content = "<b>html</b>"
        msg.metadata_json = {}
        msg.platform_thread_id = "thread-123"
        msg.content_type = "html"

        await adapter.send_message(msg)

        sent_email = adapter._smtp_client.send_message.call_args[0][0]
        assert sent_email["In-Reply-To"] == "thread-123"
        assert sent_email["References"] == "thread-123"

        # Verify multipart/alternative structure
        assert sent_email.is_multipart()
        parts = list(sent_email.iter_parts())
        assert len(parts) == 2
        assert parts[0].get_content_type() == "text/plain"
        assert parts[1].get_content_type() == "text/html"
        # Plain text fallback should have HTML stripped
        assert "<b>" not in parts[0].get_content()
        assert "html" in parts[0].get_content()
        # HTML part should preserve the original
        assert "<b>html</b>" in parts[1].get_content()

    @pytest.mark.asyncio
    @patch("gobby.communications.adapters.email.aiosmtplib", create=True)
    async def test_send_message_text_not_multipart(
        self, mock_smtp: MagicMock, adapter: EmailAdapter
    ) -> None:
        """Plain text messages should NOT be multipart."""
        adapter._smtp_client = AsyncMock()
        adapter._smtp_client.is_connected = True
        adapter._from_address = "bot@test.com"
        adapter._default_destination = "user@test.com"

        msg = MagicMock()
        msg.content = "just text"
        msg.metadata_json = {}
        msg.platform_thread_id = None
        msg.content_type = "text"

        await adapter.send_message(msg)
        sent_email = adapter._smtp_client.send_message.call_args[0][0]
        assert not sent_email.is_multipart()
        assert sent_email.get_content().strip() == "just text"

    def test_strip_html_basic(self, adapter: EmailAdapter) -> None:
        """_strip_html handles common tags."""
        html = "<p>Hello <strong>world</strong></p><br><p>Line <em>two</em></p>"
        result = adapter._strip_html(html)
        assert "Hello world" in result
        assert "Line two" in result
        assert "<" not in result

    def test_strip_html_links(self, adapter: EmailAdapter) -> None:
        """_strip_html strips <a> tags but keeps text."""
        html = 'Visit <a href="https://example.com">our site</a> today'
        result = adapter._strip_html(html)
        assert result == "Visit our site today"

    def test_strip_html_empty(self, adapter: EmailAdapter) -> None:
        assert adapter._strip_html("") == ""

    @pytest.mark.asyncio
    @patch("gobby.communications.adapters.email.aiosmtplib", create=True)
    async def test_send_attachment(
        self, mock_smtp: MagicMock, adapter: EmailAdapter, tmp_path: Path
    ) -> None:
        adapter._smtp_client = AsyncMock()
        adapter._smtp_client.is_connected = True
        adapter._from_address = "bot@test.com"
        adapter._default_destination = "user@test.com"

        file_path = tmp_path / "test.txt"
        file_path.write_bytes(b"attachment content")

        msg = MagicMock()
        msg.content = "see attached"
        msg.metadata_json = {}
        msg.platform_thread_id = None

        attachment = MagicMock()
        attachment.filename = "test.txt"
        attachment.content_type = "text/plain"

        msg_id = await adapter.send_attachment(msg, attachment, file_path)
        assert msg_id is not None

        adapter._smtp_client.send_message.assert_called_once()
        sent_email = adapter._smtp_client.send_message.call_args[0][0]

        # Verify multipart
        assert sent_email.is_multipart()
        parts = list(sent_email.iter_parts())
        assert len(parts) == 2  # Text body + attachment
        assert parts[1].get_filename() == "test.txt"
        assert parts[1].get_payload(decode=True) == b"attachment content"

    @pytest.mark.asyncio
    @patch("gobby.communications.adapters.email.aioimaplib", create=True)
    async def test_poll_no_messages(self, mock_imap: MagicMock, adapter: EmailAdapter) -> None:
        adapter._imap_client = AsyncMock()
        # Mock search to return empty response
        adapter._imap_client.search.return_value = SimpleNamespace(result="OK", lines=[b""])

        messages = await adapter.poll()
        assert messages == []
        adapter._imap_client.search.assert_called_with("UNSEEN")

    @pytest.mark.asyncio
    @patch("gobby.communications.adapters.email.aioimaplib", create=True)
    async def test_poll_with_messages(self, mock_imap: MagicMock, adapter: EmailAdapter) -> None:
        adapter._imap_client = AsyncMock()

        # poll() now calls imap.select("INBOX") before search
        adapter._imap_client.select.return_value = SimpleNamespace(result="OK", lines=[])
        # Mock search to return msg numbers
        adapter._imap_client.search.return_value = SimpleNamespace(result="OK", lines=[b"1 2"])

        # Craft two raw RFC822 emails
        msg1 = EmailMessage()
        msg1["Message-ID"] = "msg1@test"
        msg1["From"] = "user@test.com"
        msg1["Subject"] = "Test 1"
        msg1.set_content("plain text content")

        msg2 = EmailMessage()
        msg2["Message-ID"] = "msg2@test"
        msg2["From"] = "other@test.com"
        msg2.set_content("HTML fallback as plain text")
        msg2.add_alternative("<b>HTML</b>", subtype="html")

        # poll() now uses string num_str (decoded from bytes) for fetch/store
        def fetch_side_effect(num: str, query: str) -> SimpleNamespace:
            if num == "1":
                return SimpleNamespace(result="OK", lines=[b"1 (RFC822)", bytes(msg1)])
            if num == "2":
                return SimpleNamespace(result="OK", lines=[b"2 (RFC822)", bytes(msg2)])
            return SimpleNamespace(result="BAD", lines=[])

        adapter._imap_client.fetch = AsyncMock(side_effect=fetch_side_effect)
        adapter._imap_client.store = AsyncMock(return_value=SimpleNamespace(result="OK", lines=[]))

        messages = await adapter.poll()
        assert len(messages) == 2

        assert messages[0].platform_message_id == "msg1@test"
        assert messages[0].content.strip() == "plain text content"
        assert messages[0].content_type == "text"
        assert messages[0].identity_id == "user@test.com"
        assert messages[0].metadata_json["subject"] == "Test 1"

        assert messages[1].platform_message_id == "msg2@test"
        assert "HTML" in messages[1].content

        assert adapter._imap_client.store.call_count == 2
        adapter._imap_client.store.assert_any_call("1", "+FLAGS", "(\\Seen)")

    @pytest.mark.asyncio
    async def test_shutdown(self, adapter: EmailAdapter) -> None:
        smtp_mock = AsyncMock()
        imap_mock = AsyncMock()
        adapter._smtp_client = smtp_mock
        adapter._imap_client = imap_mock

        await adapter.shutdown()

        smtp_mock.quit.assert_called_once()
        imap_mock.close.assert_called_once()
        imap_mock.logout.assert_called_once()

        assert adapter._smtp_client is None
        assert adapter._imap_client is None

    @pytest.mark.asyncio
    async def test_shutdown_with_no_clients(self, adapter: EmailAdapter) -> None:
        """Shutdown with None clients should complete without error."""
        assert adapter._smtp_client is None
        assert adapter._imap_client is None
        await adapter.shutdown()
        assert adapter._smtp_client is None
        assert adapter._imap_client is None

    def test_capabilities(self, adapter: EmailAdapter) -> None:
        caps = adapter.capabilities()
        assert caps.threading is True
        assert caps.reactions is False
        assert caps.files is True
        assert caps.max_message_length == 100000

    def test_parse_webhook_raises(self, adapter: EmailAdapter) -> None:
        with pytest.raises(NotImplementedError):
            adapter.parse_webhook(b"", {})

    def test_verify_webhook(self, adapter: EmailAdapter) -> None:
        assert adapter.verify_webhook(b"", {}, "") is False


@pytest.fixture
def oauth2_config() -> MagicMock:
    mock_config = MagicMock()
    mock_config.config_json = {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "from_address": "bot@gmail.com",
        "auth_method": "oauth2",
        "oauth2_client_id": "$secret:OAUTH_CLIENT_ID",
        "oauth2_client_secret": "$secret:OAUTH_CLIENT_SECRET",
        "oauth2_refresh_token": "$secret:OAUTH_REFRESH_TOKEN",
        "oauth2_token_url": "https://oauth2.googleapis.com/token",
    }
    return mock_config


def oauth2_resolver(ref: str) -> str | None:
    secrets = {
        "$secret:OAUTH_CLIENT_ID": "test-client-id",
        "$secret:OAUTH_CLIENT_SECRET": "test-client-secret",
        "$secret:OAUTH_REFRESH_TOKEN": "test-refresh-token",
    }
    return secrets.get(ref)


class TestEmailOAuth2:
    @pytest.mark.asyncio
    @patch("gobby.communications.adapters.email.aiosmtplib", create=True)
    @patch("gobby.communications.adapters.email.aioimaplib", create=True)
    @patch("gobby.communications.adapters.email.httpx")
    async def test_initialize_oauth2(
        self,
        mock_httpx: MagicMock,
        mock_imap: MagicMock,
        mock_smtp: MagicMock,
        adapter: EmailAdapter,
        oauth2_config: MagicMock,
    ) -> None:
        """OAuth2 init exchanges refresh token for access token."""
        mock_smtp_client = AsyncMock()
        mock_smtp_client.execute_command.return_value = SimpleNamespace(code=235, message="OK")
        mock_smtp.SMTP.return_value = mock_smtp_client
        mock_imap_client = AsyncMock()
        mock_imap.IMAP4_SSL.return_value = mock_imap_client
        mock_imap_client.xoauth2.return_value = SimpleNamespace(result="OK", lines=[])

        # Mock token exchange
        mock_http_client = AsyncMock()
        mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_token_response = MagicMock()
        mock_token_response.status_code = 200
        mock_token_response.json.return_value = {
            "access_token": "test-access-token",
            "expires_in": 3600,
        }
        mock_http_client.post.return_value = mock_token_response

        await adapter.initialize(oauth2_config, oauth2_resolver)

        assert adapter._auth_method == "oauth2"
        assert adapter._oauth2_access_token == "test-access-token"
        assert adapter._oauth2_client_id == "test-client-id"
        # SMTP should use XOAUTH2 instead of login
        mock_smtp_client.login.assert_not_called()
        mock_smtp_client.execute_command.assert_called_once()
        auth_call = mock_smtp_client.execute_command.call_args[0][0]
        assert auth_call.startswith(b"AUTH XOAUTH2 ")

    @pytest.mark.asyncio
    async def test_initialize_oauth2_missing_client_id(
        self, adapter: EmailAdapter, oauth2_config: MagicMock
    ) -> None:
        """Missing OAuth2 client_id raises ValueError."""
        oauth2_config.config_json["oauth2_client_id"] = "$secret:MISSING"

        def bad_resolver(ref: str) -> str | None:
            if ref == "$secret:MISSING":
                return None
            return oauth2_resolver(ref)

        with pytest.raises(ValueError, match="OAuth2 client_id is required"):
            await adapter.initialize(oauth2_config, bad_resolver)

    def test_build_xoauth2_string(self, adapter: EmailAdapter) -> None:
        """XOAUTH2 string follows RFC 7628 format."""
        import base64

        adapter._from_address = "user@gmail.com"
        result = adapter._build_xoauth2_string("my-token")
        decoded = base64.b64decode(result).decode()
        assert decoded == "user=user@gmail.com\x01auth=Bearer my-token\x01\x01"

    @pytest.mark.asyncio
    async def test_get_oauth2_token_cached(self, adapter: EmailAdapter) -> None:
        """Cached token returned when not expired."""
        import time

        adapter._oauth2_access_token = "cached-token"
        adapter._oauth2_token_expiry = time.time() + 600  # Still valid

        token = await adapter._get_oauth2_token()
        assert token == "cached-token"

    @pytest.mark.asyncio
    @patch("gobby.communications.adapters.email.httpx")
    async def test_get_oauth2_token_refreshes_when_expired(
        self, mock_httpx: MagicMock, adapter: EmailAdapter
    ) -> None:
        """Expired token triggers refresh."""
        adapter._oauth2_access_token = "old-token"
        adapter._oauth2_token_expiry = 0  # Already expired
        adapter._oauth2_client_id = "cid"
        adapter._oauth2_client_secret = "csec"
        adapter._oauth2_refresh_token = "rtoken"
        adapter._oauth2_token_url = "https://example.com/token"

        mock_http_client = AsyncMock()
        mock_httpx.AsyncClient.return_value.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_httpx.AsyncClient.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new-token",
            "expires_in": 3600,
        }
        mock_http_client.post.return_value = mock_response

        token = await adapter._get_oauth2_token()

        assert token == "new-token"
        assert adapter._oauth2_access_token == "new-token"

    def test_password_auth_still_default(self, adapter: EmailAdapter) -> None:
        """Default auth_method is 'password'."""
        assert adapter._auth_method == "password"


def _imap_response(result: str, lines: list[bytes]) -> aioimaplib.Response:
    return aioimaplib.Response(result, lines)


@pytest.fixture
def mock_secret_resolver() -> Callable[[str], str | None]:
    def _resolve(secret_ref: str) -> str | None:
        if secret_ref == "$secret:EMAIL_PASSWORD":
            return "pass123"
        return None

    return _resolve


@pytest.mark.asyncio
async def test_send_message_with_threading(
    adapter: EmailAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    """send_message() sets In-Reply-To and References headers when thread_id is present."""
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
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
        created_at=_TIMESTAMP,
    )

    msg_id = await adapter.send_message(msg)
    assert msg_id is not None

    sent_msg = mock_smtp_inst.send_message.call_args[0][0]
    assert sent_msg["In-Reply-To"] == "<original-msg-id@example.com>"
    assert sent_msg["References"] == "<original-msg-id@example.com>"


def test_capabilities_reports_threading(adapter: EmailAdapter) -> None:
    """Email adapter capabilities should report threading=True."""
    caps = adapter.capabilities()
    assert caps.threading is True
    assert caps.reactions is False


def test_parse_webhook(adapter: EmailAdapter) -> None:
    with pytest.raises(NotImplementedError):
        adapter.parse_webhook({}, {})


def test_verify_webhook(adapter: EmailAdapter) -> None:
    assert not adapter.verify_webhook(b"", {}, "")


@pytest.mark.asyncio
async def test_poll_marks_messages_as_seen(
    adapter: EmailAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    """poll() marks each fetched message with the \\Seen flag."""
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
        config_json={"from_address": "bot@example.com", "imap_host": "imap.example.com"},
    )

    with patch("aiosmtplib.SMTP"), patch("aioimaplib.IMAP4_SSL") as MockIMAP:
        mock_imap_inst = AsyncMock()
        mock_imap_inst.login.return_value = _imap_response("OK", [b"Authenticated"])
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
async def test_poll_does_not_mark_unparsed_fetch_as_seen(
    adapter: EmailAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
        config_json={"from_address": "bot@example.com", "imap_host": "imap.example.com"},
    )

    with patch("aiosmtplib.SMTP"), patch("aioimaplib.IMAP4_SSL") as MockIMAP:
        mock_imap_inst = AsyncMock()
        mock_imap_inst.login.return_value = _imap_response("OK", [b"Authenticated"])
        MockIMAP.return_value = mock_imap_inst
        await adapter.initialize(config, mock_secret_resolver)

        mock_imap_inst.search.return_value = _imap_response("OK", [b"1"])
        mock_imap_inst.fetch.return_value = _imap_response("OK", [b"1 (FLAGS (\\Recent))", b")"])

        messages = await adapter.poll()

        assert messages == []
        mock_imap_inst.store.assert_not_called()


@pytest.mark.asyncio
async def test_imap_oauth2_login_uses_aioimaplib_xoauth2(adapter: EmailAdapter) -> None:
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
async def test_imap_oauth2_login_rejects_non_ok_response(adapter: EmailAdapter) -> None:
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
async def test_smtp_oauth2_login_rejects_non_success_response(adapter: EmailAdapter) -> None:
    adapter._auth_method = "oauth2"
    adapter._from_address = "bot@example.com"
    adapter._oauth2_access_token = "access-token"
    adapter._oauth2_token_expiry = 9999999999
    smtp_client = AsyncMock()
    smtp_client.execute_command.return_value = aiosmtplib.SMTPResponse(535, "Invalid credentials")

    with pytest.raises(ValueError, match="SMTP XOAUTH2 login failed"):
        await adapter._smtp_login(smtp_client)


@pytest.mark.asyncio
async def test_imap_password_login_rejects_non_ok_response(adapter: EmailAdapter) -> None:
    adapter._auth_method = "password"
    adapter._from_address = "bot@example.com"
    adapter._password = "pass123"
    imap_client = AsyncMock()
    imap_client.login.return_value = _imap_response("NO", [b"Invalid credentials"])

    with pytest.raises(ValueError, match="IMAP login failed: NO"):
        await adapter._imap_login(imap_client)


@pytest.mark.asyncio
async def test_initialize_rejects_plaintext_smtp_credentials(
    adapter: EmailAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
        config_json={
            "from_address": "bot@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": 25,
        },
    )

    with patch("aiosmtplib.SMTP"):
        with pytest.raises(ValueError, match="Refusing to send email credentials"):
            await adapter.initialize(config, mock_secret_resolver)


@pytest.mark.asyncio
async def test_initialize_allows_plaintext_smtp_credentials_with_override(
    adapter: EmailAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
        config_json={
            "from_address": "bot@example.com",
            "smtp_host": "smtp.example.com",
            "smtp_port": 25,
            "allow_plaintext_credentials": True,
        },
    )

    with patch("aiosmtplib.SMTP") as MockSMTP:
        mock_smtp_inst = AsyncMock()
        MockSMTP.return_value = mock_smtp_inst
        await adapter.initialize(config, mock_secret_resolver)

    MockSMTP.assert_called_once_with(
        hostname="smtp.example.com",
        port=25,
        use_tls=False,
        start_tls=False,
    )
    assert adapter._allow_plaintext_credentials is True
    assert adapter._smtp_client is mock_smtp_inst
    mock_smtp_inst.login.assert_called_once_with("bot@example.com", "pass123")


@pytest.mark.asyncio
async def test_imap_reconnect_catches_abort(
    adapter: EmailAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
        config_json={"from_address": "bot@example.com", "imap_host": "imap.example.com"},
    )

    with patch("aioimaplib.IMAP4_SSL") as MockIMAP:
        old_imap = AsyncMock()
        old_imap.login.return_value = _imap_response("OK", [b"Authenticated"])
        old_imap.noop.side_effect = aioimaplib.Abort("connection aborted")
        new_imap = AsyncMock()
        new_imap.login.return_value = _imap_response("OK", [b"Authenticated"])
        MockIMAP.side_effect = [old_imap, new_imap]

        await adapter.initialize(config, mock_secret_resolver)
        await adapter._ensure_imap_connected()

    old_imap.logout.assert_called_once()
    new_imap.wait_hello_from_server.assert_called_once()
    new_imap.login.assert_called_once_with("bot@example.com", "pass123")
    assert adapter._imap_client is new_imap


@pytest.mark.asyncio
async def test_reconnect_checks_are_lock_guarded(adapter: EmailAdapter) -> None:
    class RecordingAsyncLock(asyncio.Lock):
        def __init__(self) -> None:
            super().__init__()
            self.enter_count = 0

        async def __aenter__(self) -> None:
            self.enter_count += 1

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            tb: TracebackType | None,
        ) -> None:
            return None

    smtp_lock = RecordingAsyncLock()
    imap_lock = RecordingAsyncLock()

    async def recording_retry(
        coro_factory: Callable[[], Awaitable[Any]],
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ) -> Any:
        return await coro_factory()

    adapter._smtp_client = AsyncMock()
    adapter._smtp_client.is_connected = True
    adapter._imap_client = AsyncMock()
    adapter._smtp_connection_lock = smtp_lock
    adapter._imap_connection_lock = imap_lock

    with patch.object(adapter, "_retry", recording_retry):
        await adapter._ensure_smtp_connected()
        await adapter._ensure_imap_connected()

    assert smtp_lock.enter_count == 1
    assert imap_lock.enter_count == 1


@pytest.mark.asyncio
async def test_poll_rejects_comma_separated_from_injection(
    adapter: EmailAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
        config_json={"from_address": "bot@example.com", "imap_host": "imap.example.com"},
    )

    with patch("aiosmtplib.SMTP"), patch("aioimaplib.IMAP4_SSL") as MockIMAP:
        mock_imap_inst = AsyncMock()
        mock_imap_inst.login.return_value = _imap_response("OK", [b"Authenticated"])
        MockIMAP.return_value = mock_imap_inst
        await adapter.initialize(config, mock_secret_resolver)

        mock_imap_inst.search.return_value = _imap_response("OK", [b"1"])
        email_content = (
            b"From: user@example.com, attacker@example.com\r\n"
            b"Subject: Test\r\n"
            b"Message-ID: <m1>\r\n\r\n"
            b"Body"
        )
        mock_imap_inst.fetch.return_value = _imap_response(
            "OK", [b"1 (RFC822 {100})", email_content, b")"]
        )

        messages = await adapter.poll()

    assert messages == []
    mock_imap_inst.store.assert_not_called()


@pytest.mark.asyncio
async def test_poll_decodes_body_using_part_charset(
    adapter: EmailAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
        config_json={"from_address": "bot@example.com", "imap_host": "imap.example.com"},
    )

    with patch("aiosmtplib.SMTP"), patch("aioimaplib.IMAP4_SSL") as MockIMAP:
        mock_imap_inst = AsyncMock()
        mock_imap_inst.login.return_value = _imap_response("OK", [b"Authenticated"])
        MockIMAP.return_value = mock_imap_inst
        await adapter.initialize(config, mock_secret_resolver)

        mock_imap_inst.search.return_value = _imap_response("OK", [b"1"])
        email_content = (
            b"From: user@example.com\r\n"
            b"Subject: Test\r\n"
            b"Message-ID: <m1>\r\n"
            b"Content-Type: text/plain; charset=iso-8859-1\r\n\r\n"
            b"caf\xe9"
        )
        mock_imap_inst.fetch.return_value = _imap_response(
            "OK", [b"1 (RFC822 {100})", email_content, b")"]
        )
        mock_imap_inst.store.return_value = _imap_response("OK", [b""])

        messages = await adapter.poll()

    assert len(messages) == 1
    assert messages[0].content == "caf\xe9"


@pytest.mark.asyncio
async def test_poll_returns_already_marked_messages_after_mark_seen_failure(
    adapter: EmailAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
        config_json={"from_address": "bot@example.com", "imap_host": "imap.example.com"},
    )

    with patch("aiosmtplib.SMTP"), patch("aioimaplib.IMAP4_SSL") as MockIMAP:
        mock_imap_inst = AsyncMock()
        mock_imap_inst.login.return_value = _imap_response("OK", [b"Authenticated"])
        MockIMAP.return_value = mock_imap_inst
        await adapter.initialize(config, mock_secret_resolver)

        async def single_try_retry(
            coro_factory: Callable[[], Awaitable[Any]],
            max_retries: int = 3,
            backoff_base: float = 0.5,
        ) -> Any:
            return await coro_factory()

        mock_imap_inst.search.return_value = _imap_response("OK", [b"1 2"])
        first_email = b"From: user@example.com\r\nMessage-ID: <m1>\r\n\r\nFirst"
        second_email = b"From: user@example.com\r\nMessage-ID: <m2>\r\n\r\nSecond"
        mock_imap_inst.fetch.side_effect = [
            _imap_response("OK", [b"1 (RFC822 {50})", first_email, b")"]),
            _imap_response("OK", [b"2 (RFC822 {50})", second_email, b")"]),
        ]
        mock_imap_inst.store.side_effect = [
            _imap_response("OK", [b""]),
            _imap_response("NO", [b"store failed"]),
        ]

        with patch.object(adapter, "_retry", single_try_retry):
            messages = await adapter.poll()

    assert [message.platform_message_id for message in messages] == ["<m1>"]
    assert mock_imap_inst.store.call_count == 2


@pytest.mark.asyncio
async def test_smtp_reconnect_on_failure(
    adapter: EmailAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    """_ensure_smtp_connected() reconnects when NOOP fails."""
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
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
async def test_smtp_reconnect_on_noop_exception(
    adapter: EmailAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    """_ensure_smtp_connected() reconnects when NOOP raises an exception."""
    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
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
async def test_send_attachment_uses_async_file_read(
    adapter: EmailAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    """send_attachment() reads file bytes via asyncio.to_thread."""
    from gobby.communications.models import CommsAttachment

    config = ChannelConfig(
        id="test",
        channel_type="email",
        name="test",
        enabled=True,
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
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
        created_at=_TIMESTAMP,
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
