"""Telegram sticker vision-description tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.ai.registry import AICapability
from gobby.ai.vision import VisionExtractResult
from gobby.communications.models import CommsAttachment, CommsMessage
from gobby.communications.sticker_vision import apply_sticker_vision


def _message() -> CommsMessage:
    return CommsMessage(
        id="sticker-message",
        channel_id="telegram-channel",
        direction="inbound",
        content="",
        content_type="attachment",
        metadata_json={
            "telegram_sticker": {
                "format": "animated",
                "emoji": "🦡",
                "set_name": "quartz_badger",
                "type": "regular",
            }
        },
        created_at=datetime(2026, 7, 24, tzinfo=UTC),
    )


def _attachment(path: Path, content_type: str) -> CommsAttachment:
    return CommsAttachment(
        id=f"attachment-{path.name}",
        message_id="sticker-message",
        filename=path.name,
        content_type=content_type,
        size_bytes=path.stat().st_size,
        local_path=str(path),
    )


@pytest.mark.asyncio
async def test_sticker_thumbnail_description_becomes_responder_content(tmp_path: Path) -> None:
    original = tmp_path / "sticker.tgs"
    original.write_bytes(b"animation")
    thumbnail = tmp_path / "thumbnail.jpg"
    thumbnail.write_bytes(b"image")
    service = MagicMock()
    service.extract = AsyncMock(
        return_value=VisionExtractResult(
            text="A cheerful badger polishing a quartz crystal.",
            capability=AICapability.VISION_EXTRACT,
            provider="claude",
            model="sonnet",
        )
    )
    message = _message()

    await apply_sticker_vision(
        message,
        [
            _attachment(original, "application/x-tgsticker"),
            _attachment(thumbnail, "image/jpeg"),
        ],
        service,
    )

    assert message.content == ("Telegram sticker 🦡: A cheerful badger polishing a quartz crystal.")
    assert message.metadata_json["sticker_vision_status"] == "completed"
    assert message.metadata_json["sticker_vision_description"] == (
        "A cheerful badger polishing a quartz crystal."
    )
    assert message.metadata_json["sticker_vision_provider"] == "claude"
    assert message.metadata_json["sticker_vision_model"] == "sonnet"
    request = service.extract.await_args.args[0]
    assert request.image_path == str(thumbnail)
    assert request.caller == "communications.telegram.sticker"
    assert "animated Telegram sticker" in (request.context or "")
    assert "🦡" in (request.context or "")


@pytest.mark.asyncio
async def test_sticker_without_available_vision_uses_emoji_fallback(tmp_path: Path) -> None:
    sticker = tmp_path / "sticker.webp"
    sticker.write_bytes(b"image")
    message = _message()
    message.metadata_json["telegram_sticker"]["format"] = "static"
    service = MagicMock(is_available=False)
    service.extract = AsyncMock()

    await apply_sticker_vision(
        message,
        [_attachment(sticker, "image/webp")],
        service,
    )

    assert message.content == "Telegram sticker 🦡"
    assert message.metadata_json["sticker_vision_status"] == "unavailable"
    service.extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_sticker_without_image_preview_fails_safely(tmp_path: Path) -> None:
    sticker = tmp_path / "sticker.webm"
    sticker.write_bytes(b"video")
    service = MagicMock()
    service.extract = AsyncMock()
    message = _message()
    message.metadata_json["telegram_sticker"]["format"] = "video"

    await apply_sticker_vision(
        message,
        [_attachment(sticker, "video/webm")],
        service,
    )

    assert message.content == "Telegram sticker 🦡"
    assert message.metadata_json["sticker_vision_status"] == "unsupported"
    service.extract.assert_not_awaited()


@pytest.mark.asyncio
async def test_sticker_vision_failure_does_not_drop_message(tmp_path: Path) -> None:
    sticker = tmp_path / "sticker.webp"
    sticker.write_bytes(b"image")
    service = MagicMock()
    service.extract = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    message = _message()
    message.metadata_json["telegram_sticker"]["format"] = "static"

    await apply_sticker_vision(
        message,
        [_attachment(sticker, "image/webp")],
        service,
    )

    assert message.content == "Telegram sticker 🦡"
    assert message.metadata_json["sticker_vision_status"] == "failed"


@pytest.mark.asyncio
async def test_sticker_vision_timeout_uses_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sticker = tmp_path / "sticker.webp"
    sticker.write_bytes(b"image")
    service = MagicMock()

    async def slow_extract(*_args: object, **_kwargs: object) -> VisionExtractResult:
        await asyncio.Event().wait()
        return VisionExtractResult(
            text="late",
            capability=AICapability.VISION_EXTRACT,
            provider="test",
            model="test",
        )

    service.extract = AsyncMock(side_effect=slow_extract)
    message = _message()
    message.metadata_json["telegram_sticker"]["format"] = "static"
    monkeypatch.setattr(
        "gobby.communications.sticker_vision._STICKER_VISION_TIMEOUT_SECONDS",
        0.001,
    )

    await apply_sticker_vision(
        message,
        [_attachment(sticker, "image/webp")],
        service,
    )

    assert message.content == "Telegram sticker 🦡"
    assert message.metadata_json["sticker_vision_status"] == "failed"
