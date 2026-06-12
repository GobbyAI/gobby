"""Telegram communication channel adapter."""

from __future__ import annotations

import asyncio
import functools
import hmac
import json
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
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


class TelegramAdapter(BaseChannelAdapter):
    """Adapter for the Telegram Bot API."""

    def __init__(self) -> None:
        """Initialize the Telegram adapter."""
        self._client: httpx.AsyncClient | None = None
        self._bot_token: str | None = None
        self._api_base: str | None = None
        self._offset: int = 0

    def _redact_bot_token(self, value: str) -> str:
        """Replace the resolved Telegram bot token in error strings."""
        if not self._bot_token:
            return value
        return value.replace(self._bot_token, "***")

    def _redacted_status_error(self, exc: httpx.HTTPStatusError) -> httpx.HTTPStatusError:
        request = exc.request
        redacted_request = httpx.Request(
            request.method,
            self._redact_bot_token(str(request.url)),
            headers=request.headers,
        )
        redacted_response = httpx.Response(
            exc.response.status_code,
            headers=exc.response.headers,
            request=redacted_request,
        )
        return httpx.HTTPStatusError(
            self._redact_bot_token(str(exc)),
            request=redacted_request,
            response=redacted_response,
        )

    def _raise_for_status_with_redacted_token(self, response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise self._redacted_status_error(exc) from None

    @property
    def channel_type(self) -> str:
        """The unique type identifier for this channel."""
        return "telegram"

    @property
    def max_message_length(self) -> int:
        """Maximum message length supported by the platform."""
        return 4096

    @property
    def supports_webhooks(self) -> bool:
        """Whether this adapter supports inbound webhooks."""
        return True

    @property
    def supports_polling(self) -> bool:
        """Whether this adapter supports message polling."""
        return True

    async def initialize(
        self, config: ChannelConfig, secret_resolver: Callable[[str], str | None]
    ) -> None:
        """Set up API clients, validate credentials."""
        token_ref = config.config_json.get("bot_token")
        if not token_ref:
            raise ValueError("Telegram bot_token not found in channel config")

        if token_ref.startswith("$secret:"):
            secret_key = token_ref.replace("$secret:", "")
            self._bot_token = secret_resolver(secret_key)
        else:
            self._bot_token = token_ref

        if not self._bot_token:
            raise ValueError("Could not resolve Telegram bot token")

        self._api_base = f"https://api.telegram.org/bot{self._bot_token}"
        self._client = httpx.AsyncClient(timeout=30.0)

        # Optionally call setWebhook if webhook_base_url is configured
        webhook_base_url = config.config_json.get("webhook_base_url")
        if webhook_base_url:
            webhook_url = f"{webhook_base_url.rstrip('/')}/api/comms/webhooks/{config.name}"

            payload: dict[str, Any] = {"url": webhook_url}

            webhook_secret = config.webhook_secret
            if webhook_secret:
                payload["secret_token"] = webhook_secret

            response = await self._client.post(f"{self._api_base}/setWebhook", json=payload)
            self._raise_for_status_with_redacted_token(response)
            logger.info("Successfully registered Telegram webhook")
        else:
            # If polling is intended, delete webhook
            response = await self._client.post(f"{self._api_base}/deleteWebhook")
            self._raise_for_status_with_redacted_token(response)
            logger.info("Cleared Telegram webhook for polling mode")

    async def send_message(self, message: CommsMessage) -> str | None:
        """Send message and return platform message ID."""
        if not self._client or not self._api_base:
            raise RuntimeError("Adapter not initialized")
        client = self._client

        chat_id = self.platform_destination(message)

        chunks = self.chunk_message(message.content)

        last_message_id = None
        for chunk in chunks:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
            }

            if message.platform_thread_id:
                payload["reply_to_message_id"] = message.platform_thread_id

            url = f"{self._api_base}/sendMessage"
            try:
                response = await self._retry_request(
                    functools.partial(client.post, url, json=payload)
                )
            except httpx.HTTPStatusError as exc:
                raise self._redacted_status_error(exc) from None

            data = response.json()
            if data.get("ok"):
                last_message_id = str(data["result"]["message_id"])

        return last_message_id

    async def send_attachment(
        self, message: CommsMessage, attachment: CommsAttachment, file_path: Path
    ) -> str | None:
        """Send a file via Telegram sendDocument API."""
        if not self._client or not self._api_base:
            raise RuntimeError("Adapter not initialized")
        client = self._client

        chat_id = self.platform_destination(message)

        file_bytes = await asyncio.to_thread(file_path.read_bytes)
        files = {"document": (attachment.filename, file_bytes, attachment.content_type)}
        data: dict[str, Any] = {"chat_id": chat_id}
        if message.content:
            data["caption"] = message.content[:1024]
        if message.platform_thread_id:
            data["reply_to_message_id"] = message.platform_thread_id

        url = f"{self._api_base}/sendDocument"
        try:
            response = await self._retry_request(
                functools.partial(client.post, url, data=data, files=files)
            )
        except httpx.HTTPStatusError as exc:
            raise self._redacted_status_error(exc) from None
        result = response.json()
        if result.get("ok"):
            return str(result["result"]["message_id"])
        return None

    async def shutdown(self) -> None:
        """Cleanly close connections."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def capabilities(self) -> ChannelCapabilities:
        """Return channel capabilities."""
        return ChannelCapabilities(
            threading=True,
            reactions=False,
            files=True,
            markdown=True,
            max_message_length=self.max_message_length,
        )

    def parse_webhook(
        self, payload: dict[str, Any] | bytes, headers: dict[str, str]
    ) -> list[CommsMessage]:
        """Normalize inbound webhook payload."""
        if isinstance(payload, bytes):
            payload_dict = json.loads(payload)
        else:
            payload_dict = payload

        if "message" not in payload_dict:
            return []

        msg_data = payload_dict["message"]
        text = msg_data.get("text")
        if not text:
            # We only support text messages currently
            return []

        chat = msg_data.get("chat", {})
        raw_chat_id = chat.get("id")
        chat_id = str(raw_chat_id) if raw_chat_id is not None else ""
        raw_msg_id = msg_data.get("message_id")
        message_id = str(raw_msg_id) if raw_msg_id is not None else ""

        from_user = msg_data.get("from", {})
        raw_user_id = from_user.get("id")
        user_id = str(raw_user_id) if raw_user_id is not None else ""
        username = from_user.get("username")

        metadata = {
            "chat_id": chat_id,
            "platform_channel_id": chat_id,
            "user_id": user_id,
            "username": username,
        }

        platform_thread_id = (
            str(msg_data.get("message_thread_id"))
            if msg_data.get("message_thread_id")
            else message_id
        )

        return [
            CommsMessage(
                id=str(uuid.uuid4()),
                channel_id="",  # Will be set by the orchestrator
                direction="inbound",
                content=text,
                content_type="text",
                platform_message_id=message_id,
                platform_thread_id=platform_thread_id,
                metadata_json=metadata,
                created_at=datetime.now(UTC).isoformat(),
            )
        ]

    def verify_webhook(self, payload: bytes, headers: dict[str, str], secret: str) -> bool:
        """Verify webhook signature."""
        # Telegram sends the secret token in the lowercased header name or as defined by the user
        header_secret = headers.get("x-telegram-bot-api-secret-token")
        if not header_secret:
            return False

        return hmac.compare_digest(header_secret, secret)

    async def poll(self) -> list[CommsMessage]:
        """Call getUpdates with offset tracking for polling fallback."""
        if not self._client or not self._api_base:
            raise RuntimeError("Adapter not initialized")

        response = await self._client.get(
            f"{self._api_base}/getUpdates", params={"offset": self._offset, "timeout": 30}
        )
        self._raise_for_status_with_redacted_token(response)

        data = response.json()
        if not data.get("ok"):
            return []

        updates = data.get("result", [])
        messages = []

        for update in updates:
            update_id = update["update_id"]
            if update_id >= self._offset:
                self._offset = update_id + 1

            msg_list = self.parse_webhook(update, {})
            messages.extend(msg_list)

        return messages


# Register the adapter
register_adapter("telegram", TelegramAdapter)
