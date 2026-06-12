"""Email channel adapter."""

from __future__ import annotations

import asyncio
import base64
import email
import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import EmailMessage, Message
from email.utils import getaddresses, make_msgid
from pathlib import Path
from typing import Any

import httpx

from gobby.communications.adapters import register_adapter
from gobby.communications.adapters.base import BaseChannelAdapter
from gobby.communications.models import (
    ChannelCapabilities,
    ChannelConfig,
    CommsAttachment,
    CommsMessage,
)

logger = logging.getLogger(__name__)

HAS_SMTP = False
try:
    import aiosmtplib

    HAS_SMTP = True
except ImportError:
    pass

HAS_IMAP = False
try:
    import aioimaplib

    HAS_IMAP = True
except ImportError:
    pass


class EmailAdapter(BaseChannelAdapter):
    """Adapter for Email using SMTP for sending and IMAP for receiving."""

    def __init__(self) -> None:
        super().__init__()
        self._smtp_client: aiosmtplib.SMTP | None = None
        self._imap_client: aioimaplib.IMAP4_SSL | None = None
        self._smtp_host: str = ""
        self._smtp_port: int = 587
        self._imap_host: str = ""
        self._imap_port: int = 993
        self._from_address: str = ""
        self._to_address: str | None = None
        self._default_destination: str | None = None
        self._password: str = ""
        self._allow_plaintext_credentials = False
        self._smtp_connection_lock = asyncio.Lock()
        self._imap_connection_lock = asyncio.Lock()
        # OAuth2 fields
        self._auth_method: str = "password"
        self._oauth2_client_id: str = ""
        self._oauth2_client_secret: str = ""
        self._oauth2_token_url: str = ""
        self._oauth2_refresh_token: str = ""
        self._oauth2_access_token: str = ""
        self._oauth2_token_expiry: float = 0.0

    @property
    def channel_type(self) -> str:
        """The unique type identifier for this channel."""
        return "email"

    @property
    def max_message_length(self) -> int:
        """Maximum message length supported by the platform."""
        return 100000

    @property
    def supports_webhooks(self) -> bool:
        """Whether this adapter supports inbound webhooks."""
        return False

    @property
    def supports_polling(self) -> bool:
        """Whether this adapter supports message polling."""
        return True

    async def initialize(
        self, config: ChannelConfig, secret_resolver: Callable[[str], str | None]
    ) -> None:
        """Set up API clients, validate credentials."""
        try:
            self._smtp_host = config.config_json.get("smtp_host", "")
            self._smtp_port = config.config_json.get("smtp_port", 587)
            self._imap_host = config.config_json.get("imap_host", "")
            self._imap_port = config.config_json.get("imap_port", 993)
            self._from_address = config.config_json.get("from_address", "")
            self._to_address = config.config_json.get("to_address", "")
            self._default_destination = (
                config.config_json.get("default_recipient") or self._to_address or None
            )
            self._allow_plaintext_credentials = bool(
                config.config_json.get("allow_plaintext_credentials", False)
            )

            self._auth_method = config.config_json.get("auth_method", "password")

            if self._auth_method == "oauth2":
                await self._init_oauth2(config, secret_resolver)
            else:
                await self._init_password(config, secret_resolver)

            if HAS_SMTP and self._smtp_host:
                self._validate_smtp_credential_transport()
                self._smtp_client = aiosmtplib.SMTP(
                    hostname=self._smtp_host,
                    port=self._smtp_port,
                    use_tls=self._smtp_port == 465,
                    start_tls=self._smtp_port == 587,
                )
                await self._smtp_client.connect()
                await self._smtp_login(self._smtp_client)

            if HAS_IMAP and self._imap_host:
                self._imap_client = aioimaplib.IMAP4_SSL(host=self._imap_host, port=self._imap_port)
                await self._imap_client.wait_hello_from_server()
                await self._imap_login(self._imap_client)
        except Exception:
            await self.shutdown()
            raise

    async def _init_password(
        self, config: ChannelConfig, secret_resolver: Callable[[str], str | None]
    ) -> None:
        """Initialize password-based auth credentials."""
        password_ref = config.config_json.get("password", "$secret:EMAIL_PASSWORD")
        password = (
            secret_resolver(password_ref) if password_ref.startswith("$secret:") else password_ref
        )
        if not password:
            raise ValueError(f"Could not resolve Email password: {password_ref}")
        self._password = password

    async def _init_oauth2(
        self, config: ChannelConfig, secret_resolver: Callable[[str], str | None]
    ) -> None:
        """Initialize OAuth2 credentials and fetch initial access token."""

        def _resolve(key: str, label: str) -> str:
            ref = config.config_json.get(key, "")
            val = secret_resolver(ref) if ref.startswith("$secret:") else ref
            if not val:
                raise ValueError(f"OAuth2 {label} is required but not configured")
            return val

        self._oauth2_client_id = _resolve("oauth2_client_id", "client_id")
        self._oauth2_client_secret = _resolve("oauth2_client_secret", "client_secret")
        self._oauth2_refresh_token = _resolve("oauth2_refresh_token", "refresh_token")
        self._oauth2_token_url = config.config_json.get(
            "oauth2_token_url", "https://oauth2.googleapis.com/token"
        )

        # Fetch initial access token
        await self._refresh_oauth2_token()

    async def _refresh_oauth2_token(self) -> str:
        """Exchange refresh token for a new access token."""
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                self._oauth2_token_url,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._oauth2_client_id,
                    "client_secret": self._oauth2_client_secret,
                    "refresh_token": self._oauth2_refresh_token,
                },
            )
            if response.status_code != 200:
                raise ValueError(
                    f"OAuth2 token exchange failed (HTTP {response.status_code}): {response.text}"
                )
            data = response.json()
            self._oauth2_access_token = data["access_token"]
            expires_in = data.get("expires_in", 3600)
            # Refresh 60s before actual expiry to avoid races
            self._oauth2_token_expiry = time.time() + expires_in - 60
            logger.info("OAuth2 access token refreshed, expires in %ds", expires_in)
            return self._oauth2_access_token

    async def _get_oauth2_token(self) -> str:
        """Get a valid access token, refreshing if expired."""
        if time.time() >= self._oauth2_token_expiry:
            return await self._refresh_oauth2_token()
        return self._oauth2_access_token

    def _build_xoauth2_string(self, access_token: str) -> str:
        """Build XOAUTH2 SASL auth string per RFC 7628."""
        auth_str = f"user={self._from_address}\x01auth=Bearer {access_token}\x01\x01"
        return base64.b64encode(auth_str.encode()).decode()

    def _validate_smtp_credential_transport(self) -> None:
        """Reject SMTP credential auth over plaintext unless explicitly enabled."""
        if self._smtp_port in (465, 587) or self._allow_plaintext_credentials:
            return
        raise ValueError(
            "Refusing to send email credentials over plaintext SMTP; "
            "use port 465/587 or set allow_plaintext_credentials=true"
        )

    @staticmethod
    def _smtp_response_code(response: Any) -> int | None:
        code = getattr(response, "code", None)
        if isinstance(code, int):
            return code
        if isinstance(response, tuple) and response and isinstance(response[0], int):
            return response[0]
        return None

    async def _smtp_login(self, smtp_client: aiosmtplib.SMTP) -> None:
        """Authenticate SMTP with password or XOAUTH2."""
        if self._auth_method == "oauth2":
            token = await self._get_oauth2_token()
            auth_string = self._build_xoauth2_string(token)
            response = await smtp_client.execute_command(b"AUTH XOAUTH2 " + auth_string.encode())
            code = self._smtp_response_code(response)
            if code != 235:
                raise ValueError(f"SMTP XOAUTH2 login failed: {response!r}")
        else:
            await smtp_client.login(self._from_address, self._password)

    async def _imap_login(self, imap_client: aioimaplib.IMAP4_SSL) -> None:
        """Authenticate IMAP with password or XOAUTH2."""
        if self._auth_method == "oauth2":
            token = await self._get_oauth2_token()
            response = await imap_client.xoauth2(self._from_address, token.encode())
            auth_label = "IMAP XOAUTH2"
        else:
            response = await imap_client.login(self._from_address, self._password)
            auth_label = "IMAP"
        if response.result != "OK":
            raise ValueError(f"{auth_label} login failed: {response.result}")

    @staticmethod
    def _message_bytes_from_fetch_lines(lines: list[Any]) -> bytes | None:
        """Extract the RFC822 payload from aioimaplib's flat fetch response lines."""
        for index, line in enumerate(lines):
            if not isinstance(line, bytes) or b"RFC822" not in line:
                continue
            if index + 1 < len(lines):
                payload = lines[index + 1]
                if isinstance(payload, bytes):
                    return payload
        return None

    async def _ensure_smtp_connected(self) -> None:
        """Ensure SMTP connection is active."""
        if not HAS_SMTP or not self._smtp_client:
            return

        async def _check_and_reconnect() -> None:
            try:
                if self._smtp_client is None:
                    raise RuntimeError("SMTP client not initialized")
                if not self._smtp_client.is_connected:
                    raise RuntimeError("SMTP not connected")
                await self._smtp_client.noop()
            except (OSError, RuntimeError):
                if self._smtp_client:
                    try:
                        self._smtp_client.close()
                    except OSError:
                        pass

                self._validate_smtp_credential_transport()
                self._smtp_client = aiosmtplib.SMTP(
                    hostname=self._smtp_host,
                    port=self._smtp_port,
                    use_tls=self._smtp_port == 465,
                    start_tls=self._smtp_port == 587,
                )
                await self._smtp_client.connect()
                await self._smtp_login(self._smtp_client)

        async with self._smtp_connection_lock:
            await self._retry(_check_and_reconnect)

    async def _ensure_imap_connected(self) -> None:
        """Ensure IMAP connection is active."""
        if not HAS_IMAP or not self._imap_client:
            return

        async def _check_and_reconnect() -> None:
            try:
                # No robust is_connected check in aioimaplib besides trying a command
                if self._imap_client is None:
                    raise RuntimeError("IMAP client not initialized")
                await self._imap_client.noop()
            except (TimeoutError, OSError, RuntimeError, aioimaplib.Abort):
                try:
                    if self._imap_client:
                        await self._imap_client.logout()
                except (TimeoutError, OSError, aioimaplib.Abort):
                    pass
                self._imap_client = aioimaplib.IMAP4_SSL(host=self._imap_host, port=self._imap_port)
                await self._imap_client.wait_hello_from_server()
                await self._imap_login(self._imap_client)

        async with self._imap_connection_lock:
            await self._retry(_check_and_reconnect)

    @staticmethod
    def _strip_html(html: str) -> str:
        """Strip HTML tags to produce a plain text fallback.

        Handles common tags: <br> → newline, <p> → double newline,
        strips all other tags. Uses stdlib html.parser to avoid
        external dependencies.
        """
        from html.parser import HTMLParser

        class _Stripper(HTMLParser):
            def __init__(self) -> None:
                super().__init__()
                self._parts: list[str] = []

            def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
                if tag == "br":
                    self._parts.append("\n")
                elif tag == "p":
                    self._parts.append("\n\n")

            def handle_endtag(self, tag: str) -> None:
                if tag == "p":
                    self._parts.append("\n")

            def handle_data(self, data: str) -> None:
                self._parts.append(data)

            def get_text(self) -> str:
                return "".join(self._parts).strip()

        stripper = _Stripper()
        stripper.feed(html)
        return stripper.get_text()

    @staticmethod
    def _parse_single_address(header_value: str) -> tuple[str, str] | None:
        addresses = getaddresses([header_value])
        if len(addresses) != 1:
            return None
        name, addr = addresses[0]
        if not addr or "@" not in addr or "\r" in addr or "\n" in addr:
            return None
        return name, addr

    @staticmethod
    def _decode_message_part(part: Message) -> str:
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = part.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, errors="replace")
            except LookupError:
                return payload.decode("utf-8", errors="replace")
        if isinstance(payload, str):
            return payload
        return ""

    async def send_message(self, message: CommsMessage) -> str | None:
        """Send message and return platform message ID."""
        if not HAS_SMTP or not self._smtp_client:
            raise RuntimeError("Email SMTP client not initialized or aiosmtplib missing")

        await self._ensure_smtp_connected()
        smtp = self._smtp_client

        msg = EmailMessage()
        msg["Subject"] = message.metadata_json.get("subject", "Message from Gobby")
        msg["From"] = self._from_address

        target_address = (
            message.metadata_json.get("platform_destination")
            or message.metadata_json.get("to_address")
            or self._default_destination
        )
        if not target_address:
            raise ValueError("No target email address provided in config or message metadata")
        msg["To"] = target_address

        domain = self._from_address.split("@")[-1] if "@" in self._from_address else "gobby.local"
        msg_id = make_msgid(domain=domain)
        msg["Message-ID"] = msg_id

        if message.platform_thread_id:
            msg["In-Reply-To"] = message.platform_thread_id
            msg["References"] = message.platform_thread_id

        if message.content_type == "html":
            # RFC 2046: text/plain first, then text/html alternative
            plain_text = self._strip_html(message.content)
            msg.set_content(plain_text)
            msg.add_alternative(message.content, subtype="html")
        else:
            msg.set_content(message.content)

        await self._retry(lambda: smtp.send_message(msg))
        return msg_id

    async def send_attachment(
        self, message: CommsMessage, attachment: CommsAttachment, file_path: Path
    ) -> str | None:
        """Send an email with a MIME attachment."""
        if not HAS_SMTP or not self._smtp_client:
            raise RuntimeError("Email SMTP client not initialized or aiosmtplib missing")

        await self._ensure_smtp_connected()
        smtp = self._smtp_client

        msg = EmailMessage()
        msg["Subject"] = message.metadata_json.get("subject", "Message from Gobby")
        msg["From"] = self._from_address

        target_address = (
            message.metadata_json.get("platform_destination")
            or message.metadata_json.get("to_address")
            or self._default_destination
        )
        if not target_address:
            raise ValueError("No target email address provided in config or message metadata")
        msg["To"] = target_address

        domain = self._from_address.split("@")[-1] if "@" in self._from_address else "gobby.local"
        msg_id = make_msgid(domain=domain)
        msg["Message-ID"] = msg_id

        if message.platform_thread_id:
            msg["In-Reply-To"] = message.platform_thread_id
            msg["References"] = message.platform_thread_id

        msg.set_content(message.content or "")

        ct = attachment.content_type or "application/octet-stream"
        maintype, _, subtype = ct.partition("/")
        if not subtype:
            subtype = "octet-stream"
        file_data = await asyncio.to_thread(file_path.read_bytes)
        msg.add_attachment(
            file_data, maintype=maintype, subtype=subtype, filename=attachment.filename
        )

        await self._retry(lambda: smtp.send_message(msg))
        return msg_id

    async def poll(self) -> list[CommsMessage]:
        """Poll for new messages."""
        if not HAS_IMAP or not self._imap_client:
            return []

        await self._ensure_imap_connected()
        imap = self._imap_client

        async def _search_unseen() -> tuple[str, list[bytes]]:
            await imap.select("INBOX")
            response = await imap.search("UNSEEN")
            return response.result, response.lines

        status, response = await self._retry(_search_unseen)
        if status != "OK" or not response[0]:
            return []

        messages: list[CommsMessage] = []
        msg_nums = response[0].split()

        for num in msg_nums:
            num_str = num.decode()

            async def _fetch_msg(n: str = num_str) -> tuple[str, list[Any]]:
                response = await imap.fetch(n, "(RFC822)")
                return response.result, response.lines

            status, fetch_data = await self._retry(_fetch_msg)
            if status != "OK":
                continue

            msg_bytes = self._message_bytes_from_fetch_lines(fetch_data)
            if msg_bytes is None:
                continue

            email_msg = email.message_from_bytes(msg_bytes)

            msg_id = email_msg.get("Message-ID", "")
            thread_id = email_msg.get("In-Reply-To", "")
            sender = email_msg.get("From", "")
            parsed_sender = self._parse_single_address(sender)
            if parsed_sender is None:
                continue
            sender_name, external_user_id = parsed_sender
            external_username = sender_name or external_user_id
            subject = email_msg.get("Subject", "")

            content = ""
            content_type = "text"
            if email_msg.is_multipart():
                for payload_part in email_msg.walk():
                    if payload_part.get_content_type() == "text/plain":
                        content = self._decode_message_part(payload_part)
                        break
                    elif payload_part.get_content_type() == "text/html" and not content:
                        content = self._decode_message_part(payload_part)
                        content_type = "html"
            else:
                content = self._decode_message_part(email_msg)
                if email_msg.get_content_type() == "text/html":
                    content_type = "html"

            comms_message = CommsMessage(
                id=msg_id or f"{datetime.now(UTC).timestamp()}-{uuid.uuid4().hex[:8]}",
                channel_id="",
                direction="inbound",
                content=content,
                created_at=datetime.now(UTC).isoformat(),
                identity_id=external_user_id,
                platform_message_id=msg_id,
                platform_thread_id=thread_id,
                content_type=content_type,
                metadata_json={
                    "subject": subject,
                    "platform_destination": external_user_id,
                    "platform_channel_id": external_user_id,
                    "external_username": external_username,
                },
            )

            async def _mark_seen(n: str = num_str) -> tuple[str, list[Any]]:
                response = await imap.store(n, "+FLAGS", "(\\Seen)")
                if response.result != "OK":
                    raise RuntimeError(
                        f"Failed to mark email message {n} as seen: {response.result}"
                    )
                return response.result, response.lines

            try:
                await self._retry(_mark_seen)
            except Exception:
                logger.warning(
                    "Stopping email poll after failing to mark message seen", exc_info=True
                )
                return messages

            messages.append(comms_message)

        return messages

    async def shutdown(self) -> None:
        """Cleanly close connections."""
        if self._smtp_client:
            try:
                await self._smtp_client.quit()
            except Exception:
                logger.warning("Error during SMTP shutdown", exc_info=True)
            self._smtp_client = None

        if self._imap_client:
            try:
                await self._imap_client.close()
                await self._imap_client.logout()
            except Exception:
                logger.warning("Error during IMAP shutdown", exc_info=True)
            self._imap_client = None

    def capabilities(self) -> ChannelCapabilities:
        """Return channel capabilities."""
        return ChannelCapabilities(
            threading=True,
            reactions=False,
            files=True,
            markdown=False,
            max_message_length=self.max_message_length,
        )

    def parse_webhook(
        self, payload: dict[str, Any] | bytes, headers: dict[str, str]
    ) -> list[CommsMessage]:
        """Normalize inbound webhook payload."""
        raise NotImplementedError("Email adapter does not support webhooks")

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        """Verify webhook signature."""
        return False


# Register the adapter
register_adapter("email", EmailAdapter)
