"""Vision descriptions for inbound Telegram stickers."""

from __future__ import annotations

import logging
from typing import Protocol

from gobby.ai.vision import VisionExtractRequest, VisionExtractResult
from gobby.communications.models import CommsAttachment, CommsMessage

logger = logging.getLogger(__name__)


class StickerVisionService(Protocol):
    """Minimal vision capability used by communications."""

    @property
    def is_available(self) -> bool:
        """Return whether sticker vision can currently run."""

    async def extract(self, request: VisionExtractRequest) -> VisionExtractResult:
        """Describe the supplied image."""


async def apply_sticker_vision(
    message: CommsMessage,
    attachments: list[CommsAttachment],
    service: StickerVisionService | None,
) -> None:
    """Add a concise sticker description to content consumed by responder turns."""
    raw_sticker = message.metadata_json.get("telegram_sticker")
    if not isinstance(raw_sticker, dict):
        return

    emoji = raw_sticker.get("emoji")
    emoji_text = emoji if isinstance(emoji, str) and emoji else ""
    fallback = f"Telegram sticker {emoji_text}".rstrip()
    if service is None or not service.is_available:
        message.metadata_json["sticker_vision_status"] = "unavailable"
        _append_content(message, fallback)
        return

    image = next(
        (
            attachment
            for attachment in attachments
            if attachment.content_type.casefold().startswith("image/") and attachment.local_path
        ),
        None,
    )
    if image is None:
        message.metadata_json["sticker_vision_status"] = "unsupported"
        _append_content(message, fallback)
        return
    image_path = image.local_path
    if image_path is None:
        raise AssertionError("selected sticker image is missing a local path")

    format_name = raw_sticker.get("format")
    format_text = format_name if isinstance(format_name, str) else "unknown"
    context = (
        f"Describe this {format_text} Telegram sticker in one concise sentence for a chat "
        "assistant. Describe only its visual content."
    )
    if emoji_text:
        context = f"{context} Its associated emoji is {emoji_text}."

    try:
        result = await service.extract(
            VisionExtractRequest(
                image_path=image_path,
                context=context,
                caller="communications.telegram.sticker",
            )
        )
    except Exception as exc:
        logger.warning(
            "Sticker vision description failed for message %s (%s)",
            message.id,
            type(exc).__name__,
        )
        message.metadata_json["sticker_vision_status"] = "failed"
        _append_content(message, fallback)
        return

    description = result.text.strip()
    if not description:
        message.metadata_json["sticker_vision_status"] = "empty"
        _append_content(message, fallback)
        return

    message.metadata_json.update(
        {
            "sticker_vision_status": "completed",
            "sticker_vision_description": description,
            "sticker_vision_provider": result.provider,
            "sticker_vision_model": result.model,
        }
    )
    _append_content(message, f"{fallback}: {description}")


def _append_content(message: CommsMessage, sticker_text: str) -> None:
    existing = message.content.strip()
    message.content = f"{existing}\n\n{sticker_text}" if existing else sticker_text


__all__ = ["StickerVisionService", "apply_sticker_vision"]
