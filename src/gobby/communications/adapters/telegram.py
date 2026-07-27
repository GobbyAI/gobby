"""Telegram communication channel adapter."""

from __future__ import annotations

import asyncio
import functools
import hmac
import json
import logging
import re
import uuid
from collections import OrderedDict
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
from gobby.communications.commands import telegram_bot_commands
from gobby.communications.models import (
    ChannelCapabilities,
    ChannelConfig,
    CommsAttachment,
    CommsMessage,
)
from gobby.communications.telegram_callbacks import (
    TelegramCallbackRegistry,
    telegram_callback_message,
)
from gobby.communications.telegram_link_previews import (
    normalize_link_preview_options,
    resolve_link_preview_options,
)
from gobby.communications.telegram_proxy import resolve_telegram_proxy_url
from gobby.communications.telegram_stickers import telegram_sticker_attachments

if TYPE_CHECKING:
    from gobby.communications.attachments import AttachmentManager

logger = logging.getLogger(__name__)
_ALLOWED_UPDATES = ("message", "message_reaction", "message_reaction_count", "callback_query")
_MAX_TRACKED_EDIT_STATE = 1_024


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


def _message_thread_id(message: Mapping[str, Any]) -> str | None:
    value = message.get("message_thread_id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return str(value)


def _outbound_message_thread_id(value: str) -> int:
    try:
        thread_id = int(value)
    except ValueError as exc:
        raise ValueError("Telegram message_thread_id must be a positive integer") from exc
    if thread_id <= 0:
        raise ValueError("Telegram message_thread_id must be a positive integer")
    return thread_id


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


def _normalized_reaction(reaction: object) -> dict[str, str] | None:
    if not isinstance(reaction, dict):
        return None
    reaction_type = reaction.get("type")
    if reaction_type == "emoji":
        value = reaction.get("emoji")
    elif reaction_type == "custom_emoji":
        value = reaction.get("custom_emoji_id")
    elif reaction_type == "paid":
        value = "paid"
    else:
        return None
    if not isinstance(value, str) or not value:
        return None
    return {"type": str(reaction_type), "value": value}


def _normalized_reactions(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [normalized for item in value if (normalized := _normalized_reaction(item)) is not None]


def _telegram_reaction_message(payload: Mapping[str, Any]) -> CommsMessage | None:
    reaction_update = payload.get("message_reaction")
    if not isinstance(reaction_update, dict):
        return None
    chat = reaction_update.get("chat")
    if not isinstance(chat, dict):
        return None
    raw_chat_id = chat.get("id")
    raw_message_id = reaction_update.get("message_id")
    if raw_chat_id is None or raw_message_id is None:
        return None

    actor = reaction_update.get("user")
    if not isinstance(actor, dict):
        actor = reaction_update.get("actor_chat")
    if not isinstance(actor, dict):
        return None
    raw_user_id = actor.get("id")
    if raw_user_id is None:
        return None

    current = _normalized_reactions(reaction_update.get("new_reaction"))
    previous = _normalized_reactions(reaction_update.get("old_reaction"))
    added = [item for item in current if item not in previous]
    removed = [item for item in previous if item not in current]
    if added:
        action = "added"
        content = added[0]["value"]
    elif removed:
        action = "removed"
        content = f"-{removed[0]['value']}"
    else:
        return None

    chat_id = str(raw_chat_id)
    target_message_id = str(raw_message_id)
    update_id = payload.get("update_id")
    event_id = str(update_id) if isinstance(update_id, int) else str(uuid.uuid4())
    username = actor.get("username") or actor.get("title") or str(raw_user_id)
    conversation_type = chat.get("type")
    return CommsMessage(
        id=str(uuid.uuid4()),
        channel_id="",
        direction="inbound",
        content=content,
        content_type="reaction",
        platform_message_id=f"reaction:{event_id}:{target_message_id}",
        identity_id=str(raw_user_id),
        metadata_json={
            "chat_id": chat_id,
            "platform_channel_id": chat_id,
            "conversation_type": (
                conversation_type if isinstance(conversation_type, str) else "unknown"
            ),
            "conversation_reference": {"conversation_id": chat_id},
            "telegram_update_id": update_id,
            "reaction_target_message_id": target_message_id,
            "reaction_action": action,
            "reactions_added": added,
            "reactions_removed": removed,
            "external_username": username,
        },
        created_at=datetime.now(UTC),
    )


def _telegram_reaction_count_message(payload: Mapping[str, Any]) -> CommsMessage | None:
    count_update = payload.get("message_reaction_count")
    if not isinstance(count_update, dict):
        return None
    chat = count_update.get("chat")
    raw_message_id = count_update.get("message_id")
    reactions = count_update.get("reactions")
    if not isinstance(chat, dict) or raw_message_id is None or not isinstance(reactions, list):
        return None
    raw_chat_id = chat.get("id")
    if raw_chat_id is None:
        return None

    counts: list[dict[str, str | int]] = []
    for item in reactions:
        if not isinstance(item, dict):
            continue
        normalized = _normalized_reaction(item.get("type"))
        total_count = item.get("total_count")
        if normalized is None or isinstance(total_count, bool) or not isinstance(total_count, int):
            continue
        counts.append({**normalized, "total_count": total_count})
    if not counts:
        return None

    chat_id = str(raw_chat_id)
    target_message_id = str(raw_message_id)
    update_id = payload.get("update_id")
    event_id = str(update_id) if isinstance(update_id, int) else str(uuid.uuid4())
    conversation_type = chat.get("type")
    return CommsMessage(
        id=str(uuid.uuid4()),
        channel_id="",
        direction="inbound",
        content=str(counts[0]["value"]),
        content_type="reaction",
        platform_message_id=f"reaction-count:{event_id}:{target_message_id}",
        metadata_json={
            "chat_id": chat_id,
            "platform_channel_id": chat_id,
            "conversation_type": (
                conversation_type if isinstance(conversation_type, str) else "unknown"
            ),
            "conversation_reference": {"conversation_id": chat_id},
            "telegram_update_id": update_id,
            "reaction_target_message_id": target_message_id,
            "reaction_action": "count",
            "reaction_counts": counts,
        },
        created_at=datetime.now(UTC),
    )


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
        self._edit_overflow_ids: OrderedDict[tuple[str, str], list[str]] = OrderedDict()
        self._link_preview_options: dict[str, bool | str] | None = None
        self._message_link_preview_options: OrderedDict[
            tuple[str, str],
            dict[str, bool | str] | None,
        ] = OrderedDict()
        self._callback_registry = TelegramCallbackRegistry()

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
        self._link_preview_options = normalize_link_preview_options(
            config.config_json.get("link_preview_options"),
            field_name="Telegram channel link_preview_options",
        )
        self._message_link_preview_options.clear()

        self._api_base = f"https://api.telegram.org/bot{self._bot_token}"
        proxy_url = resolve_telegram_proxy_url(
            config.config_json.get("proxy_url"),
            secret_resolver,
        )
        self._client = (
            httpx.AsyncClient(timeout=30.0, proxy=proxy_url)
            if proxy_url
            else httpx.AsyncClient(timeout=30.0)
        )

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

        try:
            commands_response = await self._client.post(
                f"{self._api_base}/setMyCommands",
                json={"commands": telegram_bot_commands()},
            )
            self._raise_for_status_with_redacted_token(commands_response)
            commands_body = commands_response.json()
            if not isinstance(commands_body, dict) or commands_body.get("ok") is not True:
                description = (
                    commands_body.get("description", "unknown Telegram API error")
                    if isinstance(commands_body, dict)
                    else "invalid Telegram API response"
                )
                raise RuntimeError(f"Telegram setMyCommands failed: {description}")
        except Exception as exc:
            logger.warning(
                "Failed to synchronize Telegram bot commands: %s",
                self._redact_bot_token(str(exc)),
            )
        else:
            logger.info("Synchronized Telegram bot commands")

        # Optionally call setWebhook if webhook_base_url is configured
        webhook_base_url = config.config_json.get("webhook_base_url")
        if webhook_base_url:
            webhook_url = f"{webhook_base_url.rstrip('/')}/api/comms/webhooks/{config.name}"

            payload: dict[str, Any] = {
                "url": webhook_url,
                "allowed_updates": list(_ALLOWED_UPDATES),
            }

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
        reply_markup = None
        raw_keyboard = message.metadata_json.get("inline_keyboard")
        if raw_keyboard is not None:
            reply_markup = self._callback_registry.register_keyboard(
                raw_keyboard,
                session_id=message.session_id,
                chat_id=chat_id,
                thread_id=message.platform_thread_id,
                ttl_seconds=message.metadata_json.get("callback_ttl_seconds", 300),
            )
        link_preview_options = resolve_link_preview_options(
            self._link_preview_options,
            message.metadata_json,
        )

        message_ids: list[str] = []
        try:
            for index, chunk in enumerate(chunks):
                payload: dict[str, Any] = {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                }
                if link_preview_options is not None:
                    payload["link_preview_options"] = link_preview_options
                if reply_markup is not None and index == len(chunks) - 1:
                    payload["reply_markup"] = reply_markup

                if message.platform_thread_id:
                    payload["message_thread_id"] = _outbound_message_thread_id(
                        message.platform_thread_id
                    )

                data = await self._post_json("sendMessage", payload)
                if not data.get("ok"):
                    if reply_markup is not None:
                        self._callback_registry.discard_keyboard(reply_markup)
                    return None
                message_ids.append(str(data["result"]["message_id"]))
        except BaseException:
            if reply_markup is not None:
                self._callback_registry.discard_keyboard(reply_markup)
            raise

        if not message_ids:
            return None
        root_message_id = message_ids[0]
        message_key = (str(chat_id), root_message_id)
        if link_preview_options != self._link_preview_options:
            self._message_link_preview_options[message_key] = link_preview_options
            self._message_link_preview_options.move_to_end(message_key)
            if len(self._message_link_preview_options) > _MAX_TRACKED_EDIT_STATE:
                self._message_link_preview_options.popitem(last=False)
        if len(message_ids) > 1:
            self._edit_overflow_ids[message_key] = message_ids[1:]
            if len(self._edit_overflow_ids) > _MAX_TRACKED_EDIT_STATE:
                self._edit_overflow_ids.popitem(last=False)
        return root_message_id

    async def send_typing(self, conversation_id: str) -> None:
        """Publish Telegram's typing chat action."""
        await self._post_json(
            "sendChatAction",
            {"chat_id": conversation_id, "action": "typing"},
        )

    async def set_reaction(
        self,
        conversation_id: str,
        platform_message_id: str,
        reaction: str | None,
    ) -> None:
        """Add one standard emoji reaction or clear bot reactions."""
        try:
            message_id = int(platform_message_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Telegram reaction message ID must be an integer") from exc
        reaction_payload = [] if reaction is None else [{"type": "emoji", "emoji": reaction}]
        data = await self._post_json(
            "setMessageReaction",
            {
                "chat_id": conversation_id,
                "message_id": message_id,
                "reaction": reaction_payload,
            },
        )
        if data.get("ok") is not True:
            description = data.get("description")
            detail = description if isinstance(description, str) else "unknown Telegram error"
            raise RuntimeError(f"Telegram setMessageReaction failed: {detail}")

    async def edit_message(
        self,
        platform_message_id: str,
        content: str,
        conversation_id: str,
    ) -> None:
        """Replace a Telegram message, maintaining overflow chunks when needed."""
        chunks = markdown_to_telegram_html_chunks(content, self.max_message_length)
        message_key = (conversation_id, platform_message_id)
        target_ids = [
            platform_message_id,
            *self._edit_overflow_ids.get(message_key, []),
        ]
        link_preview_options = self._message_link_preview_options.get(
            message_key,
            self._link_preview_options,
        )

        for index, chunk in enumerate(chunks):
            if index < len(target_ids):
                payload: dict[str, Any] = {
                    "chat_id": conversation_id,
                    "message_id": target_ids[index],
                    "text": chunk,
                    "parse_mode": "HTML",
                }
                if link_preview_options is not None:
                    payload["link_preview_options"] = link_preview_options
                result = await self._post_json("editMessageText", payload)
                if not result.get("ok"):
                    description = str(result.get("description", "unknown Telegram API error"))
                    if "message is not modified" not in description.casefold():
                        raise RuntimeError(f"Telegram editMessageText failed: {description}")
                continue

            payload = {
                "chat_id": conversation_id,
                "text": chunk,
                "parse_mode": "HTML",
            }
            if link_preview_options is not None:
                payload["link_preview_options"] = link_preview_options
            result = await self._post_json(
                "sendMessage",
                payload,
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
            self._edit_overflow_ids[message_key] = overflow_ids
            self._edit_overflow_ids.move_to_end(message_key)
            if len(self._edit_overflow_ids) > _MAX_TRACKED_EDIT_STATE:
                self._edit_overflow_ids.popitem(last=False)
        else:
            self._edit_overflow_ids.pop(message_key, None)

    async def send_attachment(
        self, message: CommsMessage, attachment: CommsAttachment, file_path: Path
    ) -> str | None:
        """Send an image or document through the matching Telegram API."""
        if not self._client or not self._api_base:
            raise RuntimeError("Adapter not initialized")
        client = self._client

        chat_id = self.platform_destination(message)

        file_bytes = await asyncio.to_thread(file_path.read_bytes)
        is_voice_note = (
            attachment.content_type == "audio/ogg"
            and message.metadata_json.get("voice_note") is True
        )
        is_image = attachment.content_type.startswith("image/")
        file_field = "voice" if is_voice_note else "photo" if is_image else "document"
        method = "sendVoice" if is_voice_note else "sendPhoto" if is_image else "sendDocument"
        files = {file_field: (attachment.filename, file_bytes, attachment.content_type)}
        data: dict[str, Any] = {"chat_id": chat_id}
        if message.content and not is_voice_note:
            data["caption"] = markdown_to_telegram_html_chunks(message.content, 1024)[0]
            data["parse_mode"] = "HTML"
        if message.platform_thread_id:
            data["message_thread_id"] = _outbound_message_thread_id(message.platform_thread_id)

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
        raw_attachments = message.metadata_json.get("telegram_attachments")
        if isinstance(raw_attachments, list):
            attachment_specs = [item for item in raw_attachments if isinstance(item, dict)]
        else:
            raw_attachment = message.metadata_json.get("telegram_attachment")
            attachment_specs = [raw_attachment] if isinstance(raw_attachment, dict) else []
        if not attachment_specs:
            return []
        if not self._client or not self._api_base or not self._bot_token:
            raise RuntimeError("Adapter not initialized")

        attachments: list[CommsAttachment] = []
        try:
            for attachment_spec in attachment_specs:
                attachments.append(
                    await self._download_inbound_attachment(
                        message,
                        attachment_spec,
                        attachment_manager,
                    )
                )
        except Exception:
            await asyncio.to_thread(
                attachment_manager.delete_paths,
                [
                    attachment.local_path
                    for attachment in attachments
                    if attachment.local_path is not None
                ],
            )
            raise
        return attachments

    async def _download_inbound_attachment(
        self,
        message: CommsMessage,
        raw_attachment: dict[str, Any],
        attachment_manager: AttachmentManager,
    ) -> CommsAttachment:
        """Download and persist one normalized Telegram attachment."""
        client = self._client
        if client is None or not self._api_base or not self._bot_token:
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

        actual_suffix = Path(telegram_file_path).suffix.casefold()
        if raw_attachment.get("media_type") == "sticker_thumbnail" and actual_suffix in {
            ".jpg",
            ".jpeg",
            ".webp",
        }:
            filename = f"{Path(filename).stem}{actual_suffix}"
            content_type = "image/jpeg" if actual_suffix in {".jpg", ".jpeg"} else "image/webp"

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
        return CommsAttachment(
            id=str(uuid.uuid4()),
            message_id=message.id,
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
            local_path=str(local_path),
            platform_url=f"telegram://{telegram_file_path.lstrip('/')}",
            created_at=datetime.now(UTC),
        )

    async def shutdown(self) -> None:
        """Cleanly close connections."""
        if self._client:
            await self._client.aclose()
            self._client = None
        self._edit_overflow_ids.clear()
        self._message_link_preview_options.clear()

    def capabilities(self) -> ChannelCapabilities:
        """Return channel capabilities."""
        return ChannelCapabilities(
            threading=True,
            reactions=True,
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

        reaction_message = _telegram_reaction_message(payload_dict)
        if reaction_message is not None:
            return [reaction_message]
        reaction_count_message = _telegram_reaction_count_message(payload_dict)
        if reaction_count_message is not None:
            return [reaction_count_message]
        callback_message = telegram_callback_message(payload_dict, self._callback_registry)
        if callback_message is not None:
            return [callback_message]

        if "message" not in payload_dict:
            return []

        msg_data = payload_dict["message"]
        raw_text = msg_data.get("text")
        text = raw_text if isinstance(raw_text, str) else ""
        raw_caption = msg_data.get("caption")
        caption = raw_caption if isinstance(raw_caption, str) else ""
        raw_sticker = msg_data.get("sticker")
        sticker = telegram_sticker_attachments(msg_data)
        if raw_sticker is not None and sticker is None:
            return []
        attachment = _telegram_media_attachment(msg_data) if sticker is None else None
        if not text and attachment is None and sticker is None:
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
        platform_thread_id = _message_thread_id(msg_data)

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
        if platform_thread_id is not None:
            metadata["message_thread_id"] = platform_thread_id
            metadata["is_topic_message"] = True
        if attachment is not None:
            metadata["telegram_attachment"] = attachment
            metadata["voice_note"] = attachment.get("media_type") == "voice"
        elif sticker is not None:
            sticker_attachments, sticker_metadata = sticker
            metadata["telegram_attachments"] = sticker_attachments
            metadata["telegram_sticker"] = sticker_metadata
            metadata["voice_note"] = False

        return [
            CommsMessage(
                id=str(uuid.uuid4()),
                channel_id="",  # Will be set by the orchestrator
                direction="inbound",
                content=text if text else caption,
                content_type="attachment"
                if attachment is not None or sticker is not None
                else "text",
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
            f"{self._api_base}/getUpdates",
            params={
                "offset": self._offset,
                "timeout": 30,
                "allowed_updates": json.dumps(_ALLOWED_UPDATES),
            },
            timeout=35.0,
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
        await self._answer_callback_queries(messages)
        for message in messages:
            update_id = message.metadata_json.get("telegram_update_id")
            if isinstance(update_id, int):
                self._acknowledged_update_ids.add(update_id)

        self._advance_acknowledged_offset()
        await self._persist_poll_offset()

    async def acknowledge_webhook_messages(self, messages: list[CommsMessage]) -> None:
        """Answer callback queries delivered by webhook."""
        await self._answer_callback_queries(messages)

    async def _answer_callback_queries(self, messages: list[CommsMessage]) -> None:
        for message in messages:
            callback_id = message.metadata_json.get("telegram_callback_query_id")
            if not isinstance(callback_id, str) or not callback_id:
                continue
            status = message.metadata_json.get("callback_status")
            if status == "ok":
                text = "Selection received."
            elif status == "expired":
                text = "This action has expired."
            else:
                text = "This action is no longer available."
            await self._post_json(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": text},
            )


# Register the adapter
register_adapter("telegram", TelegramAdapter)
