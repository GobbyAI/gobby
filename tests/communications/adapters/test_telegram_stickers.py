"""Focused Telegram sticker ingestion tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import httpx
import pytest

from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.attachments import AttachmentManager


def _payload(sticker: dict[str, object]) -> dict[str, object]:
    return {
        "update_id": 10001,
        "message": {
            "message_id": 1366,
            "from": {"id": 1111111, "username": "testuser"},
            "chat": {"id": 2222222, "type": "private"},
            "sticker": sticker,
        },
    }


@pytest.mark.parametrize(
    ("format_name", "format_flags", "content_type", "extension"),
    [
        pytest.param("static", {}, "image/webp", "webp", id="static"),
        pytest.param(
            "animated",
            {"is_animated": True},
            "application/x-tgsticker",
            "tgs",
            id="animated",
        ),
        pytest.param("video", {"is_video": True}, "video/webm", "webm", id="video"),
    ],
)
def test_parse_sticker_variants(
    format_name: str,
    format_flags: dict[str, object],
    content_type: str,
    extension: str,
) -> None:
    adapter = TelegramAdapter()
    sticker = {
        "file_id": f"{format_name}-id",
        "file_unique_id": f"{format_name}-unique",
        "type": "regular",
        "width": 512,
        "height": 512,
        "emoji": "🦡",
        "set_name": "quartz_badger",
        "file_size": 512,
        "thumbnail": {
            "file_id": f"{format_name}-thumbnail-id",
            "file_unique_id": f"{format_name}-thumbnail-unique",
            "file_size": 64,
        },
        **format_flags,
    }

    messages = adapter.parse_webhook(_payload(sticker), {})

    assert len(messages) == 1
    message = messages[0]
    assert message.content == ""
    assert message.content_type == "attachment"
    assert message.metadata_json["telegram_sticker"] == {
        "format": format_name,
        "emoji": "🦡",
        "set_name": "quartz_badger",
        "type": "regular",
    }
    expected_attachments = [
        {
            "file_id": f"{format_name}-id",
            "filename": f"sticker_{format_name}-unique.{extension}",
            "content_type": content_type,
            "size_bytes": 512,
            "media_type": "sticker",
        }
    ]
    if format_name != "static":
        expected_attachments.append(
            {
                "file_id": f"{format_name}-thumbnail-id",
                "filename": f"sticker_thumbnail_{format_name}-thumbnail-unique.webp",
                "content_type": "image/webp",
                "size_bytes": 64,
                "media_type": "sticker_thumbnail",
            }
        )
    assert message.metadata_json["telegram_attachments"] == expected_attachments
    assert message.metadata_json["voice_note"] is False


def test_parse_sticker_rejects_conflicting_format_flags_safely() -> None:
    adapter = TelegramAdapter()

    messages = adapter.parse_webhook(
        _payload(
            {
                "file_id": "invalid-id",
                "file_unique_id": "invalid-unique",
                "is_animated": True,
                "is_video": True,
            }
        ),
        {},
    )

    assert messages == []


@pytest.mark.asyncio
async def test_download_animated_sticker_and_thumbnail(tmp_path: Path) -> None:
    adapter = TelegramAdapter()
    message = adapter.parse_webhook(
        _payload(
            {
                "file_id": "animated-id",
                "file_unique_id": "animated-unique",
                "is_animated": True,
                "file_size": 512,
                "thumbnail": {
                    "file_id": "thumbnail-id",
                    "file_unique_id": "thumbnail-unique",
                    "file_size": 64,
                },
            }
        ),
        {},
    )[0]
    message.id = "message-id"

    get_file_responses = [
        httpx.Response(
            200,
            json={"ok": True, "result": {"file_path": "stickers/animated.tgs"}},
            request=httpx.Request("POST", "https://api.telegram.org/getFile"),
        ),
        httpx.Response(
            200,
            json={"ok": True, "result": {"file_path": "stickers/thumbnail.jpg"}},
            request=httpx.Request("POST", "https://api.telegram.org/getFile"),
        ),
    ]
    file_responses = [
        httpx.Response(
            200,
            content=b"animated sticker",
            request=httpx.Request("GET", "https://api.telegram.org/file"),
        ),
        httpx.Response(
            200,
            content=b"thumbnail image",
            request=httpx.Request("GET", "https://api.telegram.org/file"),
        ),
    ]
    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=get_file_responses)
    mock_client.get = AsyncMock(side_effect=file_responses)
    adapter._client = mock_client
    adapter._api_base = "https://api.telegram.org/bottest-token"
    adapter._bot_token = "test-token"

    attachments = await adapter.download_inbound_attachments(
        message,
        AttachmentManager(tmp_path),
    )

    assert [(item.filename, item.content_type) for item in attachments] == [
        ("sticker_animated-unique.tgs", "application/x-tgsticker"),
        ("sticker_thumbnail_thumbnail-unique.jpg", "image/jpeg"),
    ]
    assert [Path(item.local_path or "").read_bytes() for item in attachments] == [
        b"animated sticker",
        b"thumbnail image",
    ]
    assert mock_client.post.await_args_list == [
        call(
            "https://api.telegram.org/bottest-token/getFile",
            json={"file_id": "animated-id"},
        ),
        call(
            "https://api.telegram.org/bottest-token/getFile",
            json={"file_id": "thumbnail-id"},
        ),
    ]
    assert mock_client.get.await_args_list == [
        call("https://api.telegram.org/file/bottest-token/stickers/animated.tgs"),
        call("https://api.telegram.org/file/bottest-token/stickers/thumbnail.jpg"),
    ]


@pytest.mark.asyncio
async def test_thumbnail_download_failure_removes_partial_sticker_file(tmp_path: Path) -> None:
    adapter = TelegramAdapter()
    message = adapter.parse_webhook(
        _payload(
            {
                "file_id": "animated-id",
                "file_unique_id": "animated-unique",
                "is_animated": True,
                "thumbnail": {
                    "file_id": "thumbnail-id",
                    "file_unique_id": "thumbnail-unique",
                },
            }
        ),
        {},
    )[0]

    mock_client = MagicMock()
    mock_client.post = AsyncMock(
        side_effect=[
            httpx.Response(
                200,
                json={"ok": True, "result": {"file_path": "stickers/animated.tgs"}},
                request=httpx.Request("POST", "https://api.telegram.org/getFile"),
            ),
            httpx.Response(
                200,
                json={"ok": False, "description": "thumbnail unavailable"},
                request=httpx.Request("POST", "https://api.telegram.org/getFile"),
            ),
        ]
    )
    mock_client.get = AsyncMock(
        return_value=httpx.Response(
            200,
            content=b"animated sticker",
            request=httpx.Request("GET", "https://api.telegram.org/file"),
        )
    )
    adapter._client = mock_client
    adapter._api_base = "https://api.telegram.org/bottest-token"
    adapter._bot_token = "test-token"

    with pytest.raises(RuntimeError, match="thumbnail unavailable"):
        await adapter.download_inbound_attachments(
            message,
            AttachmentManager(tmp_path),
        )

    assert [path for path in tmp_path.rglob("*") if path.is_file()] == []
