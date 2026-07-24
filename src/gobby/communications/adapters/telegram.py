"""Telegram communication channel adapter."""

from __future__ import annotations

import asyncio
import functools
import hmac
import json
import logging
import re
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from gobby.communications.adapters import register_adapter
from gobby.communications.adapters.base import BaseChannelAdapter
from gobby.communications.adapters.telegram_formatting import (
    markdown_to_telegram_html_chunks,
)
from gobby.communications.models import (
    ChannelCapabilities,
    ChannelConfig,
    CommsAttachment,
    CommsMessage,
)

if TYPE_CHECKING:
    from gobby.communications.attachments import AttachmentManager

logger = logging.getLogger(__name__)


def _mentions_telegram_bot(
    text: str,
    message: Mapping[str, Any],
    *,
    bot_username: str | None,
    bot_user_id: str | None,
) -> bool:
    if bot_username:
        pattern = rf"(?<![A-Za-z0-9_])@{re.escape(bot_username)}(?![A-Za-z0-9_])"
        if re.search(pattern, text, flags=re.IGNORECASE):
            return True
    reply_to = message.get("reply_to_message")
    if not isinstance(reply_to, Mapping):
        return False
    reply_from = reply_to.get("from")
    if not isinstance(reply_from, Mapping):
        return False
    reply_user_id = reply_from.get("id")
    return bot_user_id is not None and str(reply_user_id) == bot_user_id


def _file_size(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _telegram_media_attachment(msg_data: Mapping[str, Any]) -> dict[str, Any] | None:
    media_type: str
    media: Mapping[str, Any]
    default_content_type: str
    default_extension: str

    photos = msg_data.get("photo")
    if isinstance(photos, list):
        candidates = [
            (index, photo)
            for index, photo in enumerate(photos)
            if isinstance(photo, Mapping) and isinstance(photo.get("file_id"), str)
        ]
        if not candidates:
            return None
        _, media = max(
            candidates,
            key=lambda item: (_file_size(item[1].get("file_size")), item[0]),
        )
        media_type = "photo"
        default_content_type = "image/jpeg"
        default_extension = "jpg"
    else:
        media = {}
        media_type = ""
        default_content_type = ""
        default_extension = ""
        for candidate_type, candidate_content_type, candidate_extension in (
            ("document", "application/octet-stream", "bin"),
            ("voice", "audio/ogg", "ogg"),
            ("video", "video/mp4", "mp4"),
        ):
            candidate = msg_data.get(candidate_type)
            if isinstance(candidate, Mapping):
                media = candidate
                media_type = candidate_type
                default_content_type = candidate_content_type
                default_extension = candidate_extension
                break

    file_id = media.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None

    raw_filename = media.get("file_name")
    if isinstance(raw_filename, str) and raw_filename:
        filename = raw_filename
    else:
        unique_id = media.get("file_unique_id")
        identifier = unique_id if isinstance(unique_id, str) and unique_id else file_id
        filename = f"{media_type}_{identifier}.{default_extension}"

    raw_content_type = media.get("mime_type")
    content_type = (
        raw_content_type
        if isinstance(raw_content_type, str) and raw_content_type
        else default_content_type
    )
    return {
        "file_id": file_id,
        "filename": filename,
        "content_type": content_type,
        "size_bytes": _file_size(media.get("file_size")),
        "media_type": media_type,
    }


class TelegramAdapter(BaseChannelAdapter):
    """Adapter for the Telegram Bot API."""

    def __init__(self) -> None:
        """Initialize the Telegram adapter."""
        super().__init__()
        self._client: httpx.AsyncClient | None = None
        self._bot_token: str | None = None
        self._api_base: str | None = None
        self._bot_username: str | None = None
        self._bot_user_id: str | None = None
        self._offset: int = 0
        self._persisted_offset: int = 0
        self._pending_update_ids: list[int] = []
        self._acknowledged_update_ids: set[int] = set()
        self._edit_overflow_ids: dict[str, list[str]] = {}

    def _advance_acknowledged_offset(self) -> None:
        while self._pending_update_ids:
            update_id = self._pending_update_ids[0]
            if update_id not in self._acknowledged_update_ids:
                break
            self._offset = max(self._offset, update_id + 1)
            self._pending_update_ids.pop(0)
            self._acknowledged_update_ids.discard(update_id)

    async def _persist_poll_offset(self) -> None:
        if self._offset == self._persisted_offset:
            return
        if await self._update_channel_config({"poll_offset": self._offset}):
            self._persisted_offset = self._offset

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

    async def _post_json(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._client or not self._api_base:
            raise RuntimeError("Adapter not initialized")
        url = f"{self._api_base}/{method}"
        try:
            response = await self._retry_request(
                functools.partial(self._client.post, url, json=payload)
            )
        except httpx.HTTPStatusError as exc:
            raise self._redacted_status_error(exc) from None
        body: object = response.json()
        if not isinstance(body, dict):
            raise RuntimeError(f"Telegram {method} returned a non-object response")
        return body

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

        poll_offset = config.config_json.get("poll_offset", 0)
        if isinstance(poll_offset, bool) or not isinstance(poll_offset, int) or poll_offset < 0:
            raise ValueError("Telegram poll_offset must be a non-negative integer")
        self._offset = poll_offset
        self._persisted_offset = poll_offset

        self._api_base = f"https://api.telegram.org/bot{self._bot_token}"
        self._client = httpx.AsyncClient(timeout=30.0)

        response = await self._client.post(f"{self._api_base}/getMe")
        self._raise_for_status_with_redacted_token(response)
        body = response.json()
        if isinstance(body, dict):
            result = body.get("result")
            if isinstance(result, dict):
                username = result.get("username")
                user_id = result.get("id")
                if isinstance(username, str) and username:
                    self._bot_username = username
                if isinstance(user_id, str | int):
                    self._bot_user_id = str(user_id)

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

        chat_id = self.platform_destination(message)

        chunks = markdown_to_telegram_html_chunks(message.content, self.max_message_length)

        message_ids: list[str] = []
        for chunk in chunks:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
            }

            if message.platform_thread_id:
                payload["reply_to_message_id"] = message.platform_thread_id

            data = await self._post_json("sendMessage", payload)
            if data.get("ok"):
                message_ids.append(str(data["result"]["message_id"]))

        if not message_ids:
            return None
        root_message_id = message_ids[0]
        if len(message_ids) > 1:
            self._edit_overflow_ids[root_message_id] = message_ids[1:]
        return root_message_id

    async def send_typing(self, conversation_id: str) -> None:
        """Publish Telegram's typing chat action."""
        await self._post_json(
            "sendChatAction",
            {"chat_id": conversation_id, "action": "typing"},
        )

    async def edit_message(
        self,
        platform_message_id: str,
        content: str,
        conversation_id: str,
    ) -> None:
        """Replace a Telegram message, maintaining overflow chunks when needed."""
        chunks = markdown_to_telegram_html_chunks(content, self.max_message_length)
        target_ids = [
            platform_message_id,
            *self._edit_overflow_ids.get(platform_message_id, []),
        ]

        for index, chunk in enumerate(chunks):
            if index < len(target_ids):
                await self._post_json(
                    "editMessageText",
                    {
                        "chat_id": conversation_id,
                        "message_id": target_ids[index],
                        "text": chunk,
                        "parse_mode": "HTML",
                    },
                )
                continue

            result = await self._post_json(
                "sendMessage",
                {
                    "chat_id": conversation_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                },
            )
            if not result.get("ok"):
                raise RuntimeError("Telegram sendMessage did not return a message")
            target_ids.append(str(result["result"]["message_id"]))

        for stale_message_id in target_ids[len(chunks) :]:
            await self._post_json(
                "deleteMessage",
                {
                    "chat_id": conversation_id,
                    "message_id": stale_message_id,
                },
            )

        overflow_ids = target_ids[1 : len(chunks)]
        if overflow_ids:
            self._edit_overflow_ids[platform_message_id] = overflow_ids
        else:
            self._edit_overflow_ids.pop(platform_message_id, None)

    async def send_attachment(
        self, message: CommsMessage, attachment: CommsAttachment, file_path: Path
    ) -> str | None:
        """Send an image or document through the matching Telegram API."""
        if not self._client or not self._api_base:
            raise RuntimeError("Adapter not initialized")
        client = self._client

        chat_id = self.platform_destination(message)

        file_bytes = await asyncio.to_thread(file_path.read_bytes)
        is_image = attachment.content_type.startswith("image/")
        file_field = "photo" if is_image else "document"
        method = "sendPhoto" if is_image else "sendDocument"
        files = {file_field: (attachment.filename, file_bytes, attachment.content_type)}
        data: dict[str, Any] = {"chat_id": chat_id}
        if message.content:
            data["caption"] = markdown_to_telegram_html_chunks(message.content, 1024)[0]
            data["parse_mode"] = "HTML"
        if message.platform_thread_id:
            data["reply_to_message_id"] = message.platform_thread_id

        url = f"{self._api_base}/{method}"
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

    async def download_inbound_attachments(
        self,
        message: CommsMessage,
        attachment_manager: AttachmentManager,
    ) -> list[CommsAttachment]:
        """Download Telegram media referenced by an inbound message."""
        raw_attachment = message.metadata_json.get("telegram_attachment")
        if not isinstance(raw_attachment, dict):
            return []
        if not self._client or not self._api_base or not self._bot_token:
            raise RuntimeError("Adapter not initialized")

        file_id = raw_attachment.get("file_id")
        filename = raw_attachment.get("filename")
        content_type = raw_attachment.get("content_type")
        if not isinstance(file_id, str) or not file_id:
            raise ValueError("Telegram attachment file_id is missing")
        if not isinstance(filename, str) or not filename:
            raise ValueError("Telegram attachment filename is missing")
        if not isinstance(content_type, str) or not content_type:
            raise ValueError("Telegram attachment content_type is missing")

        client = self._client
        try:
            file_info_response = await self._retry_request(
                functools.partial(
                    client.post,
                    f"{self._api_base}/getFile",
                    json={"file_id": file_id},
                )
            )
        except httpx.HTTPStatusError as exc:
            raise self._redacted_status_error(exc) from None

        file_info = file_info_response.json()
        result = file_info.get("result")
        if not file_info.get("ok") or not isinstance(result, dict):
            description = file_info.get("description", "unknown Telegram API error")
            raise RuntimeError(f"Telegram getFile failed: {description}")
        telegram_file_path = result.get("file_path")
        if not isinstance(telegram_file_path, str) or not telegram_file_path:
            raise RuntimeError("Telegram getFile response did not include file_path")

        download_url = (
            f"https://api.telegram.org/file/bot{self._bot_token}/{telegram_file_path.lstrip('/')}"
        )
        try:
            file_response = await self._retry_request(functools.partial(client.get, download_url))
        except httpx.HTTPStatusError as exc:
            raise self._redacted_status_error(exc) from None

        size_bytes = len(file_response.content)
        if not attachment_manager.validate_size(size_bytes, self.channel_type):
            limit = attachment_manager.get_size_limit(self.channel_type)
            raise ValueError(
                f"Telegram attachment size {size_bytes} exceeds limit of {limit} bytes"
            )
        local_path = await attachment_manager.store(file_response.content, filename)
        return [
            CommsAttachment(
                id=str(uuid.uuid4()),
                message_id=message.id,
                filename=filename,
                content_type=content_type,
                size_bytes=size_bytes,
                local_path=str(local_path),
                platform_url=f"telegram://{telegram_file_path.lstrip('/')}",
                created_at=datetime.now(UTC),
            )
        ]

    async def shutdown(self) -> None:
        """Cleanly close connections."""
        if self._client:
            await self._client.aclose()
            self._client = None
        self._edit_overflow_ids.clear()

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
        raw_text = msg_data.get("text")
        text = raw_text if isinstance(raw_text, str) else ""
        raw_caption = msg_data.get("caption")
        caption = raw_caption if isinstance(raw_caption, str) else ""
        attachment = _telegram_media_attachment(msg_data)
        if not text and attachment is None:
            return []

        chat = msg_data.get("chat", {})
        raw_chat_id = chat.get("id")
        chat_id = str(raw_chat_id) if raw_chat_id is not None else ""
        raw_conversation_type = chat.get("type")
        conversation_type = (
            raw_conversation_type if isinstance(raw_conversation_type, str) else "unknown"
        )
        raw_msg_id = msg_data.get("message_id")
        message_id = str(raw_msg_id) if raw_msg_id is not None else ""

        from_user = msg_data.get("from", {})
        raw_user_id = from_user.get("id")
        user_id = str(raw_user_id) if raw_user_id is not None else ""
        username = from_user.get("username")
        if not user_id:
            return []

        metadata = {
            "chat_id": chat_id,
            "platform_channel_id": chat_id,
            "conversation_type": conversation_type,
            "mentioned": _mentions_telegram_bot(
                text if text else caption,
                msg_data,
                bot_username=self._bot_username,
                bot_user_id=self._bot_user_id,
            ),
            "conversation_reference": {"conversation_id": chat_id},
            "telegram_update_id": payload_dict.get("update_id"),
            "user_id": user_id,
            "username": username,
            "external_username": username or user_id,
        }
        if attachment is not None:
            metadata["telegram_attachment"] = attachment
            metadata["voice_note"] = attachment.get("media_type") == "voice"

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
                content=text if text else caption,
                content_type="attachment" if attachment is not None else "text",
                platform_message_id=message_id,
                platform_thread_id=platform_thread_id,
                identity_id=user_id,
                metadata_json=metadata,
                created_at=datetime.now(UTC),
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
        self._pending_update_ids = []
        self._acknowledged_update_ids.clear()

        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self._pending_update_ids.append(update_id)
            msg_list = self.parse_webhook(update, {})
            if not msg_list and isinstance(update_id, int):
                self._acknowledged_update_ids.add(update_id)
            messages.extend(msg_list)

        self._advance_acknowledged_offset()
        await self._persist_poll_offset()
        return messages

    async def acknowledge_messages(self, messages: list[CommsMessage]) -> None:
        """Advance the Telegram offset for successfully handled updates."""
        for message in messages:
            update_id = message.metadata_json.get("telegram_update_id")
            if isinstance(update_id, int):
                self._acknowledged_update_ids.add(update_id)

        self._advance_acknowledged_offset()
        await self._persist_poll_offset()


# Register the adapter
register_adapter("telegram", TelegramAdapter)
