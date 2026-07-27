"""Inbound Telegram update normalization."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from gobby.communications.models import CommsMessage
from gobby.communications.telegram_callbacks import (
    TelegramCallbackRegistry,
    telegram_callback_message,
)
from gobby.communications.telegram_stickers import telegram_sticker_attachments


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


def parse_telegram_update(
    payload: dict[str, Any] | bytes,
    *,
    bot_username: str | None,
    bot_user_id: str | None,
    callback_registry: TelegramCallbackRegistry,
) -> list[CommsMessage]:
    """Normalize an inbound Telegram update."""
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
    callback_message = telegram_callback_message(payload_dict, callback_registry)
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
            bot_username=bot_username,
            bot_user_id=bot_user_id,
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
            content_type="attachment" if attachment is not None or sticker is not None else "text",
            platform_message_id=message_id,
            platform_thread_id=platform_thread_id,
            identity_id=user_id,
            metadata_json=metadata,
            created_at=datetime.now(UTC),
        )
    ]
