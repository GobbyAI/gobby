"""Tests for the Telegram communications adapter."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
import pytest

from gobby.communications.adapters.telegram import TelegramAdapter
from gobby.communications.attachments import AttachmentManager
from gobby.communications.models import ChannelConfig, CommsAttachment, CommsMessage


@pytest.fixture
def channel_config() -> ChannelConfig:
    return ChannelConfig(
        id="test_telegram_channel",
        channel_type="telegram",
        name="Test Telegram",
        enabled=True,
        config_json={"bot_token": "$secret:TELEGRAM_BOT_TOKEN"},
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
        webhook_secret="test_secret_token",
    )


@pytest.fixture
def secret_resolver() -> Callable[[str], str | None]:
    def resolver(key: str) -> str | None:
        if key == "TELEGRAM_BOT_TOKEN":
            return "test-telegram-token"
        return None

    return resolver


@pytest.fixture
def adapter() -> TelegramAdapter:
    return TelegramAdapter()


def _telegram_api_success(result: object = True) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"ok": True, "result": result}
    return response


@pytest.mark.asyncio
async def test_initialize_success(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    mock_post = AsyncMock(return_value=_telegram_api_success())

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.post = mock_post

        # Test without webhook
        await adapter.initialize(channel_config, secret_resolver)
        MockClient.assert_called_once_with(timeout=30.0)
        assert adapter._bot_token == "test-telegram-token"
        assert adapter._api_base == "https://api.telegram.org/bottest-telegram-token"
        assert mock_post.await_args_list[:2] == [
            call("https://api.telegram.org/bottest-telegram-token/getMe"),
            call(
                "https://api.telegram.org/bottest-telegram-token/setMyCommands",
                json={
                    "commands": [
                        {"command": "new", "description": "Start a new conversation"},
                        {"command": "reset", "description": "Reset the current conversation"},
                        {"command": "stop", "description": "Stop the active response"},
                        {
                            "command": "status",
                            "description": "Show responder provider and model",
                        },
                        {
                            "command": "subscriptions",
                            "description": "Manage event subscriptions",
                        },
                        {"command": "help", "description": "Show available commands"},
                    ]
                },
            ),
        ]
        mock_post.assert_called_with(
            "https://api.telegram.org/bottest-telegram-token/deleteWebhook"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proxy_url",
    [
        "http://127.0.0.1:8080",
        "socks5://proxy-user:proxy-password@127.0.0.1:1080",
    ],
)
async def test_initialize_uses_configured_proxy_for_shared_http_client(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    proxy_url: str,
) -> None:
    channel_config.config_json["proxy_url"] = "$secret:TELEGRAM_PROXY_URL"

    def resolve_secret(key: str) -> str | None:
        return {
            "TELEGRAM_BOT_TOKEN": "test-telegram-token",
            "TELEGRAM_PROXY_URL": proxy_url,
        }.get(key)

    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.post = AsyncMock(return_value=_telegram_api_success())

        await adapter.initialize(channel_config, resolve_secret)

    MockClient.assert_called_once_with(timeout=30.0, proxy=proxy_url)
    assert adapter._api_base == "https://api.telegram.org/bottest-telegram-token"


@pytest.mark.asyncio
async def test_initialize_captures_bot_identity_for_mention_gating(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    get_me_response = MagicMock()
    get_me_response.json.return_value = {
        "ok": True,
        "result": {"id": 123456, "username": "gobby_bot"},
    }
    delete_webhook_response = MagicMock()
    commands_response = MagicMock()
    commands_response.json.return_value = {"ok": True, "result": True}
    mock_post = AsyncMock(side_effect=[get_me_response, commands_response, delete_webhook_response])

    with patch("httpx.AsyncClient") as MockClient:
        MockClient.return_value.post = mock_post

        await adapter.initialize(channel_config, secret_resolver)

    assert adapter._bot_user_id == "123456"
    assert adapter._bot_username == "gobby_bot"


@pytest.mark.asyncio
async def test_initialize_with_webhook(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    channel_config.config_json["webhook_base_url"] = "https://example.com/webhooks"

    mock_post = AsyncMock(return_value=_telegram_api_success())

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.post = mock_post

        await adapter.initialize(channel_config, secret_resolver)

        mock_post.assert_called_with(
            "https://api.telegram.org/bottest-telegram-token/setWebhook",
            json={
                "url": f"https://example.com/webhooks/api/comms/webhooks/{channel_config.name}",
                "allowed_updates": [
                    "message",
                    "message_reaction",
                    "message_reaction_count",
                    "callback_query",
                ],
                "secret_token": "test_secret_token",
            },
        )
        assert mock_post.call_count >= 1
        assert mock_post.call_args is not None


@pytest.mark.asyncio
async def test_initialize_warns_and_continues_on_set_my_commands_api_failure(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    get_me_response = MagicMock()
    get_me_response.json.return_value = {"ok": True, "result": {"id": 123}}
    commands_response = MagicMock()
    commands_response.json.return_value = {
        "ok": False,
        "description": "command registration denied",
    }
    delete_webhook_response = MagicMock()
    delete_webhook_response.json.return_value = {"ok": True}
    post = AsyncMock(side_effect=[get_me_response, commands_response, delete_webhook_response])

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.post = post

        with caplog.at_level(
            logging.WARNING,
            logger="gobby.communications.adapters.telegram",
        ):
            await adapter.initialize(channel_config, secret_resolver)

    assert post.await_count == 3
    assert post.await_args_list[1].args[0].endswith("/setMyCommands")
    assert post.await_args_list[2].args[0].endswith("/deleteWebhook")
    assert "Failed to synchronize Telegram bot commands" in caplog.text


@pytest.mark.asyncio
async def test_initialize_missing_token(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    channel_config.config_json = {}
    with pytest.raises(ValueError, match="Telegram bot_token not found"):
        await adapter.initialize(channel_config, secret_resolver)


@pytest.mark.asyncio
async def test_send_message_basic(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    mock_post = AsyncMock()

    # Mock behavior depending on the url called
    async def side_effect(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        if "deleteWebhook" in url:
            resp.json.return_value = {"ok": True}
        else:
            resp.json.return_value = {"ok": True, "result": {"message_id": 12345}}
        return resp

    mock_post.side_effect = side_effect

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.post = mock_post

        await adapter.initialize(channel_config, secret_resolver)

        message = CommsMessage(
            id="msg1",
            channel_id=channel_config.id,
            direction="outbound",
            content="Hello world",
            platform_thread_id="123",
            metadata_json={"platform_destination": "chat999"},
            created_at=datetime.now(UTC).isoformat(),
        )

        msg_id = await adapter.send_message(message)

        assert message.channel_id != message.metadata_json["platform_destination"]
        assert msg_id == "12345"

        # Checking last call
        call_args, call_kwargs = mock_post.call_args_list[-1]
        assert call_args[0] == "https://api.telegram.org/bottest-telegram-token/sendMessage"
        assert call_kwargs["json"] == {
            "chat_id": "chat999",
            "text": "Hello world",
            "parse_mode": "HTML",
            "message_thread_id": 123,
        }


@pytest.mark.asyncio
async def test_send_message_http_status_error_redacts_bot_token(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    token = "test-telegram-token"
    channel_config.config_json["proxy_url"] = "http://127.0.0.1:8080"
    mock_post = AsyncMock()

    async def side_effect(url, **kwargs):
        request = httpx.Request("POST", url)
        if "deleteWebhook" in url:
            return httpx.Response(200, request=request, json={"ok": True})
        if "getMe" in url:
            return httpx.Response(
                200,
                request=request,
                json={"ok": True, "result": {"id": 123456, "username": "gobby_bot"}},
            )
        if "setMyCommands" in url:
            return httpx.Response(200, request=request, json={"ok": True, "result": True})
        return httpx.Response(400, request=request, json={"ok": False})

    mock_post.side_effect = side_effect

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.post = mock_post

        await adapter.initialize(channel_config, secret_resolver)

        message = CommsMessage(
            id="msg1",
            channel_id=channel_config.id,
            direction="outbound",
            content="Hello world",
            metadata_json={"platform_destination": "chat999"},
            created_at=datetime.now(UTC).isoformat(),
        )

        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await adapter.send_message(message)

    exc = exc_info.value
    assert exc.response.status_code == 400
    assert token not in str(exc)
    assert token not in str(exc.request.url)
    assert "***" in str(exc)


@pytest.mark.asyncio
async def test_send_message_chunking(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    mock_post = AsyncMock()
    sent_message_ids = iter((901, 902))

    # Mock behavior depending on the url called
    async def side_effect(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        if "deleteWebhook" in url:
            resp.json.return_value = {"ok": True}
        elif "sendMessage" in url:
            resp.json.return_value = {
                "ok": True,
                "result": {"message_id": next(sent_message_ids)},
            }
        else:
            resp.json.return_value = {"ok": True, "result": {"message_id": 999}}
        return resp

    mock_post.side_effect = side_effect

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.post = mock_post

        await adapter.initialize(channel_config, secret_resolver)

        # Reset mock to only count send_message calls
        mock_post.reset_mock()
        mock_post.side_effect = side_effect  # Re-apply after reset

        long_content = "A" * 5000
        message = CommsMessage(
            id="msg1",
            channel_id=channel_config.id,
            direction="outbound",
            content=long_content,
            platform_thread_id="123",
            session_id="session-1",
            metadata_json={
                "platform_destination": "chat999",
                "callback_action": "session_action",
                "inline_keyboard": [[{"text": "Continue", "value": "Continue"}]],
            },
            created_at=datetime.now(UTC).isoformat(),
        )

        await adapter.send_message(message)

        assert mock_post.call_count == 2
        # First call with 4096 chars
        first_call_args = mock_post.call_args_list[0][1]
        assert len(first_call_args["json"]["text"]) == 4096
        assert "reply_markup" not in first_call_args["json"]
        # Second call with 904 chars
        second_call_args = mock_post.call_args_list[1][1]
        assert len(second_call_args["json"]["text"]) == 904
        assert second_call_args["json"]["reply_markup"]["inline_keyboard"][0][0]["text"] == (
            "Continue"
        )
        assert message.metadata_json["platform_message_ids"] == ["901", "902"]


@pytest.mark.asyncio
async def test_send_message_stops_after_first_failed_chunk(
    adapter: TelegramAdapter,
) -> None:
    post_json = AsyncMock(
        side_effect=[
            {"ok": True, "result": {"message_id": 100}},
            {"ok": False, "description": "send denied"},
            {"ok": True, "result": {"message_id": 102}},
        ]
    )
    adapter._client = MagicMock()
    adapter._api_base = "https://api.telegram.org/bottest-token"
    message = CommsMessage(
        id="msg1",
        channel_id="channel-1",
        direction="outbound",
        content="A" * 9000,
        metadata_json={"platform_destination": "chat999"},
        created_at=datetime.now(UTC),
    )

    with patch.object(adapter, "_post_json", post_json):
        result = await adapter.send_message(message)

    assert result is None
    assert post_json.await_count == 2


@pytest.mark.asyncio
async def test_send_message_renders_telegram_safe_html(adapter: TelegramAdapter) -> None:
    mock_client = MagicMock()

    async def post(url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"ok": True, "result": {"message_id": 12345}},
        )

    mock_client.post = AsyncMock(side_effect=post)
    adapter._client = mock_client
    adapter._api_base = "https://api.telegram.org/bottest-token"
    message = CommsMessage(
        id="msg1",
        channel_id="channel1",
        direction="outbound",
        content=("**bold** *italic* `<code>` [link](https://example.com?a=1&b=2) <raw>"),
        metadata_json={"platform_destination": "chat999"},
        created_at=datetime.now(UTC),
    )

    await adapter.send_message(message)

    payload = mock_client.post.await_args.kwargs["json"]
    assert payload == {
        "chat_id": "chat999",
        "text": (
            "<b>bold</b> <i>italic</i> <code>&lt;code&gt;</code> "
            '<a href="https://example.com?a=1&amp;b=2">link</a> &lt;raw&gt;'
        ),
        "parse_mode": "HTML",
    }


@pytest.mark.asyncio
async def test_send_message_balances_html_across_4096_character_chunks(
    adapter: TelegramAdapter,
) -> None:
    mock_client = MagicMock()
    message_id = 0

    async def post(url: str, **kwargs: object) -> httpx.Response:
        nonlocal message_id
        message_id += 1
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"ok": True, "result": {"message_id": message_id}},
        )

    mock_client.post = AsyncMock(side_effect=post)
    adapter._client = mock_client
    adapter._api_base = "https://api.telegram.org/bottest-token"
    message = CommsMessage(
        id="msg1",
        channel_id="channel1",
        direction="outbound",
        content=f"**{'A' * 5000}**",
        metadata_json={"platform_destination": "chat999"},
        created_at=datetime.now(UTC),
    )

    root_message_id = await adapter.send_message(message)

    payloads = [call.kwargs["json"] for call in mock_client.post.await_args_list]
    assert root_message_id == "1"
    assert [
        len(payload["text"].removeprefix("<b>").removesuffix("</b>")) for payload in payloads
    ] == [
        4096,
        904,
    ]
    assert all(payload["text"].startswith("<b>") for payload in payloads)
    assert all(payload["text"].endswith("</b>") for payload in payloads)
    assert adapter._edit_overflow_ids == {("chat999", "1"): ["2"]}


@pytest.mark.asyncio
async def test_send_typing_calls_send_chat_action(adapter: TelegramAdapter) -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://api.telegram.org/bottest-token/sendChatAction"),
        json={"ok": True, "result": True},
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=response)
    adapter._client = mock_client
    adapter._api_base = "https://api.telegram.org/bottest-token"

    assert adapter.supports_typing is True
    await adapter.send_typing("chat999")

    mock_client.post.assert_awaited_once_with(
        "https://api.telegram.org/bottest-token/sendChatAction",
        json={"chat_id": "chat999", "action": "typing"},
    )


@pytest.mark.asyncio
async def test_set_reaction_adds_and_clears_acknowledgement(
    adapter: TelegramAdapter,
) -> None:
    post_json = AsyncMock(return_value={"ok": True, "result": True})

    with patch.object(adapter, "_post_json", post_json):
        await adapter.set_reaction("chat999", "123", "👀")
        await adapter.set_reaction("chat999", "123", None)

    assert adapter.supports_reactions is True
    assert post_json.await_args_list == [
        call(
            "setMessageReaction",
            {
                "chat_id": "chat999",
                "message_id": 123,
                "reaction": [{"type": "emoji", "emoji": "👀"}],
            },
        ),
        call(
            "setMessageReaction",
            {
                "chat_id": "chat999",
                "message_id": 123,
                "reaction": [],
            },
        ),
    ]


@pytest.mark.asyncio
async def test_set_reaction_rejects_non_numeric_message_id(adapter: TelegramAdapter) -> None:
    with pytest.raises(ValueError, match="message ID must be an integer"):
        await adapter.set_reaction("chat999", "not-a-message", "👀")


@pytest.mark.asyncio
async def test_edit_message_calls_edit_message_text_with_html(
    adapter: TelegramAdapter,
) -> None:
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://api.telegram.org/bottest-token/editMessageText"),
        json={"ok": True, "result": {"message_id": 12345}},
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=response)
    adapter._client = mock_client
    adapter._api_base = "https://api.telegram.org/bottest-token"

    assert adapter.supports_message_edit is True
    await adapter.edit_message("12345", "**updated**", "chat999")

    mock_client.post.assert_awaited_once_with(
        "https://api.telegram.org/bottest-token/editMessageText",
        json={
            "chat_id": "chat999",
            "message_id": "12345",
            "text": "<b>updated</b>",
            "parse_mode": "HTML",
        },
    )


@pytest.mark.asyncio
async def test_edit_message_treats_not_modified_as_success(
    adapter: TelegramAdapter,
) -> None:
    adapter._client = MagicMock()
    adapter._api_base = "https://api.telegram.org/bottest-token"
    post_json = AsyncMock(
        return_value={
            "ok": False,
            "description": "Bad Request: message is not modified",
        }
    )

    with patch.object(adapter, "_post_json", post_json):
        await adapter.edit_message("12345", "unchanged", "chat999")

    assert post_json.await_count == 1
    post_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_attachment_uses_send_photo_for_images(
    adapter: TelegramAdapter,
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"image bytes")
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://api.telegram.org/bottest-token/sendPhoto"),
        json={"ok": True, "result": {"message_id": 42}},
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=response)
    adapter._client = mock_client
    adapter._api_base = "https://api.telegram.org/bottest-token"
    message = CommsMessage(
        id="msg1",
        channel_id="channel1",
        direction="outbound",
        content="**caption**",
        metadata_json={"platform_destination": "chat999"},
        created_at=datetime.now(UTC),
    )
    attachment = CommsAttachment(
        id="attachment1",
        message_id=message.id,
        filename=image_path.name,
        content_type="image/jpeg",
        size_bytes=image_path.stat().st_size,
    )

    result = await adapter.send_attachment(message, attachment, image_path)

    assert result == "42"
    call = mock_client.post.await_args
    assert call.args[0] == "https://api.telegram.org/bottest-token/sendPhoto"
    assert call.kwargs["data"] == {
        "chat_id": "chat999",
        "caption": "<b>caption</b>",
        "parse_mode": "HTML",
    }
    assert call.kwargs["files"] == {
        "photo": ("photo.jpg", b"image bytes", "image/jpeg"),
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_send_attachment_sends_remaining_caption_chunks(
    adapter: TelegramAdapter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"image bytes")
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://api.telegram.org/bottest-token/sendPhoto"),
        json={"ok": True, "result": {"message_id": 42}},
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=response)
    adapter._client = mock_client
    adapter._api_base = "https://api.telegram.org/bottest-token"
    post_json = AsyncMock(return_value={"ok": True, "result": {"message_id": 43}})
    monkeypatch.setattr(adapter, "_post_json", post_json)
    long_caption = "a" * 1500
    message = CommsMessage(
        id="msg1",
        channel_id="channel1",
        direction="outbound",
        content=long_caption,
        metadata_json={"platform_destination": "chat999"},
        created_at=datetime.now(UTC),
    )
    attachment = CommsAttachment(
        id="attachment1",
        message_id=message.id,
        filename=image_path.name,
        content_type="image/jpeg",
        size_bytes=image_path.stat().st_size,
    )

    result = await adapter.send_attachment(message, attachment, image_path)

    assert result == "42"
    posted = mock_client.post.await_args
    assert posted is not None
    caption = posted.kwargs["data"]["caption"]
    assert caption != long_caption
    post_json.assert_awaited_once()
    follow_call = post_json.await_args
    assert follow_call is not None
    follow = follow_call.args[1]
    assert follow["chat_id"] == "chat999"
    assert follow["parse_mode"] == "HTML"
    assert follow["text"]
    assert caption + follow["text"] == long_caption


@pytest.mark.asyncio
async def test_send_attachment_reports_partial_caption_when_follow_up_fails(
    adapter: TelegramAdapter,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"image bytes")
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://api.telegram.org/bottest-token/sendPhoto"),
        json={"ok": True, "result": {"message_id": 42}},
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=response)
    adapter._client = mock_client
    adapter._api_base = "https://api.telegram.org/bottest-token"
    monkeypatch.setattr(
        adapter, "_post_json", AsyncMock(return_value={"ok": False, "description": "fail"})
    )
    message = CommsMessage(
        id="msg1",
        channel_id="channel1",
        direction="outbound",
        content="a" * 1500,
        metadata_json={"platform_destination": "chat999"},
        created_at=datetime.now(UTC),
    )
    attachment = CommsAttachment(
        id="attachment1",
        message_id=message.id,
        filename=image_path.name,
        content_type="image/jpeg",
        size_bytes=image_path.stat().st_size,
    )

    with pytest.raises(RuntimeError, match="caption continuation failed after media message 42"):
        await adapter.send_attachment(message, attachment, image_path)


@pytest.mark.asyncio
async def test_send_attachment_uses_send_voice_for_voice_notes(
    adapter: TelegramAdapter,
    tmp_path: Path,
) -> None:
    voice_path = tmp_path / "reply.ogg"
    voice_path.write_bytes(b"OggSvoice")
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://api.telegram.org/bottest-token/sendVoice"),
        json={"ok": True, "result": {"message_id": 43}},
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=response)
    adapter._client = mock_client
    adapter._api_base = "https://api.telegram.org/bottest-token"
    message = CommsMessage(
        id="msg-voice",
        channel_id="channel1",
        direction="outbound",
        content="spoken reply",
        platform_thread_id="17",
        metadata_json={
            "platform_destination": "chat999",
            "voice_note": True,
        },
        created_at=datetime.now(UTC),
    )
    attachment = CommsAttachment(
        id="attachment-voice",
        message_id=message.id,
        filename=voice_path.name,
        content_type="audio/ogg",
        size_bytes=voice_path.stat().st_size,
    )

    result = await adapter.send_attachment(message, attachment, voice_path)

    assert result == "43"
    call = mock_client.post.await_args
    assert call.args[0] == "https://api.telegram.org/bottest-token/sendVoice"
    assert call.kwargs["data"] == {
        "chat_id": "chat999",
        "message_thread_id": 17,
    }
    assert call.kwargs["files"] == {
        "voice": ("reply.ogg", b"OggSvoice", "audio/ogg"),
    }


def test_parse_webhook(adapter: TelegramAdapter) -> None:
    payload = {
        "update_id": 10000,
        "message": {
            "message_id": 1365,
            "from": {"id": 1111111, "is_bot": False, "first_name": "Test", "username": "testuser"},
            "chat": {
                "id": 2222222,
                "first_name": "Test",
                "username": "testuser",
                "type": "private",
            },
            "date": 1441645532,
            "text": "/start",
        },
    }

    messages = adapter.parse_webhook(payload, {})
    assert len(messages) == 1

    msg = messages[0]
    assert msg.direction == "inbound"
    assert msg.content == "/start"
    assert msg.platform_message_id == "1365"
    assert msg.platform_thread_id is None
    assert msg.metadata_json["user_id"] == "1111111"
    assert msg.metadata_json["username"] == "testuser"
    assert msg.metadata_json["chat_id"] == "2222222"
    assert msg.metadata_json["platform_channel_id"] == "2222222"
    assert msg.metadata_json["conversation_type"] == "private"
    assert msg.metadata_json["mentioned"] is False
    assert msg.metadata_json["conversation_reference"] == {
        "conversation_id": "2222222",
    }


@pytest.mark.parametrize("payload", [b"{", b"[]"])
def test_parse_webhook_rejects_malformed_or_non_mapping_json(
    adapter: TelegramAdapter,
    payload: bytes,
) -> None:
    assert adapter.parse_webhook(payload, {}) == []


def test_parse_webhook_tolerates_explicit_null_message_sections(
    adapter: TelegramAdapter,
) -> None:
    payload = {
        "update_id": 10001,
        "message": {
            "message_id": 1366,
            "from": {"id": 1111111},
            "chat": None,
            "text": "hello",
        },
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    assert messages[0].metadata_json["chat_id"] == ""


def test_parse_group_webhook_sets_conversation_reference(adapter: TelegramAdapter) -> None:
    adapter._bot_username = "gobby_bot"
    payload = {
        "update_id": 10001,
        "message": {
            "message_id": 1366,
            "from": {"id": 1111111, "is_bot": False, "username": "testuser"},
            "chat": {
                "id": -1002222222,
                "title": "Test group",
                "type": "supergroup",
            },
            "date": 1441645532,
            "text": "@gobby_bot hello group",
        },
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    assert messages[0].metadata_json["conversation_reference"] == {
        "conversation_id": "-1002222222",
    }
    assert messages[0].metadata_json["conversation_type"] == "supergroup"
    assert messages[0].metadata_json["mentioned"] is True


def test_parse_group_webhook_marks_unmentioned_message(adapter: TelegramAdapter) -> None:
    adapter._bot_username = "gobby_bot"
    payload = {
        "update_id": 10002,
        "message": {
            "message_id": 1367,
            "from": {"id": 1111111, "is_bot": False, "username": "testuser"},
            "chat": {
                "id": -1002222222,
                "title": "Test group",
                "type": "supergroup",
            },
            "date": 1441645532,
            "text": "hello @someone_else",
        },
    }

    messages = adapter.parse_webhook(payload, {})

    assert messages[0].metadata_json["mentioned"] is False


def test_parse_message_reaction_normalizes_added_emoji(adapter: TelegramAdapter) -> None:
    messages = adapter.parse_webhook(
        {
            "update_id": 10003,
            "message_reaction": {
                "chat": {"id": -1002222222, "type": "supergroup"},
                "message_id": 1400,
                "user": {"id": 1111111, "username": "testuser"},
                "old_reaction": [],
                "new_reaction": [{"type": "emoji", "emoji": "👍"}],
            },
        },
        {},
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.content_type == "reaction"
    assert message.content == "👍"
    assert message.platform_message_id == "reaction:10003:1400"
    assert message.identity_id == "1111111"
    assert message.metadata_json["reaction_target_message_id"] == "1400"
    assert message.metadata_json["reaction_action"] == "added"
    assert message.metadata_json["reactions_added"] == [{"type": "emoji", "value": "👍"}]
    assert message.metadata_json["reactions_removed"] == []


def test_parse_message_reaction_normalizes_removed_custom_emoji(
    adapter: TelegramAdapter,
) -> None:
    messages = adapter.parse_webhook(
        {
            "update_id": 10004,
            "message_reaction": {
                "chat": {"id": -1002222222, "type": "supergroup"},
                "message_id": 1401,
                "actor_chat": {"id": -1002222222, "title": "Test group"},
                "old_reaction": [{"type": "custom_emoji", "custom_emoji_id": "custom-123"}],
                "new_reaction": [],
            },
        },
        {},
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.content == "-custom-123"
    assert message.identity_id == "-1002222222"
    assert message.metadata_json["reaction_action"] == "removed"
    assert message.metadata_json["reactions_removed"] == [
        {"type": "custom_emoji", "value": "custom-123"}
    ]


def test_parse_message_reaction_count_normalizes_anonymous_totals(
    adapter: TelegramAdapter,
) -> None:
    messages = adapter.parse_webhook(
        {
            "update_id": 10005,
            "message_reaction_count": {
                "chat": {"id": -1002222222, "type": "supergroup"},
                "message_id": 1402,
                "reactions": [
                    {
                        "type": {"type": "emoji", "emoji": "🔥"},
                        "total_count": 3,
                    },
                    {
                        "type": {
                            "type": "custom_emoji",
                            "custom_emoji_id": "custom-456",
                        },
                        "total_count": 2,
                    },
                ],
            },
        },
        {},
    )

    assert len(messages) == 1
    message = messages[0]
    assert message.platform_message_id == "reaction-count:10005:1402"
    assert message.identity_id is None
    assert message.metadata_json["reaction_action"] == "count"
    assert message.metadata_json["reaction_counts"] == [
        {"type": "emoji", "value": "🔥", "total_count": 3},
        {"type": "custom_emoji", "value": "custom-456", "total_count": 2},
    ]


@pytest.mark.parametrize(
    ("media", "caption", "expected_attachment"),
    [
        pytest.param(
            {
                "photo": [
                    {
                        "file_id": "photo-small",
                        "file_unique_id": "photo-small-unique",
                        "file_size": 100,
                    },
                    {
                        "file_id": "photo-large",
                        "file_unique_id": "photo-large-unique",
                        "file_size": 400,
                    },
                ]
            },
            "photo caption",
            {
                "file_id": "photo-large",
                "filename": "photo_photo-large-unique.jpg",
                "content_type": "image/jpeg",
                "size_bytes": 400,
                "media_type": "photo",
            },
            id="photo",
        ),
        pytest.param(
            {
                "document": {
                    "file_id": "document-id",
                    "file_unique_id": "document-unique",
                    "file_name": "report.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 512,
                }
            },
            "document caption",
            {
                "file_id": "document-id",
                "filename": "report.pdf",
                "content_type": "application/pdf",
                "size_bytes": 512,
                "media_type": "document",
            },
            id="document",
        ),
        pytest.param(
            {
                "voice": {
                    "file_id": "voice-id",
                    "file_unique_id": "voice-unique",
                    "mime_type": "audio/ogg",
                    "file_size": 256,
                }
            },
            "",
            {
                "file_id": "voice-id",
                "filename": "voice_voice-unique.ogg",
                "content_type": "audio/ogg",
                "size_bytes": 256,
                "media_type": "voice",
            },
            id="voice",
        ),
        pytest.param(
            {
                "video": {
                    "file_id": "video-id",
                    "file_unique_id": "video-unique",
                    "mime_type": "video/mp4",
                    "file_size": 1024,
                }
            },
            "video caption",
            {
                "file_id": "video-id",
                "filename": "video_video-unique.mp4",
                "content_type": "video/mp4",
                "size_bytes": 1024,
                "media_type": "video",
            },
            id="video",
        ),
    ],
)
def test_parse_webhook_media(
    adapter: TelegramAdapter,
    media: dict[str, object],
    caption: str,
    expected_attachment: dict[str, object],
) -> None:
    payload = {
        "update_id": 10001,
        "message": {
            "message_id": 1366,
            "from": {"id": 1111111, "username": "testuser"},
            "chat": {"id": 2222222, "type": "private"},
            "caption": caption,
            **media,
        },
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    assert messages[0].content == caption
    assert messages[0].content_type == "attachment"
    assert messages[0].metadata_json["telegram_attachment"] == expected_attachment
    assert messages[0].metadata_json["voice_note"] is (expected_attachment["media_type"] == "voice")


@pytest.mark.asyncio
async def test_download_inbound_attachments_uses_get_file_and_attachment_storage(
    adapter: TelegramAdapter,
    tmp_path: Path,
) -> None:
    payload = {
        "message": {
            "message_id": 1366,
            "from": {"id": 1111111, "username": "testuser"},
            "chat": {"id": 2222222, "type": "private"},
            "caption": "document caption",
            "document": {
                "file_id": "document-id",
                "file_unique_id": "document-unique",
                "file_name": "report.pdf",
                "mime_type": "application/pdf",
                "file_size": 512,
            },
        }
    }
    message = adapter.parse_webhook(payload, {})[0]
    message.id = "message-id"

    get_file_response = httpx.Response(
        200,
        json={"ok": True, "result": {"file_path": "documents/report.pdf"}},
        request=httpx.Request("POST", "https://api.telegram.org/getFile"),
    )
    file_response = httpx.Response(
        200,
        content=b"downloaded report",
        request=httpx.Request("GET", "https://api.telegram.org/file"),
    )
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=get_file_response)
    mock_client.get = AsyncMock(return_value=file_response)
    adapter._client = mock_client
    adapter._api_base = "https://api.telegram.org/bottest-token"
    adapter._bot_token = "test-token"

    attachments = await adapter.download_inbound_attachments(
        message,
        AttachmentManager(tmp_path),
    )

    assert len(attachments) == 1
    attachment = attachments[0]
    assert attachment.message_id == "message-id"
    assert attachment.filename == "report.pdf"
    assert attachment.content_type == "application/pdf"
    assert attachment.size_bytes == len(b"downloaded report")
    assert attachment.platform_url == "telegram://documents/report.pdf"
    assert attachment.local_path is not None
    local_path = Path(attachment.local_path)
    assert tmp_path in local_path.parents
    assert local_path.read_bytes() == b"downloaded report"
    mock_client.post.assert_awaited_once_with(
        "https://api.telegram.org/bottest-token/getFile",
        json={"file_id": "document-id"},
    )
    mock_client.get.assert_awaited_once_with(
        "https://api.telegram.org/file/bottest-token/documents/report.pdf"
    )


def test_verify_webhook(adapter: TelegramAdapter) -> None:
    assert (
        adapter.verify_webhook(b"", {"x-telegram-bot-api-secret-token": "secret123"}, "secret123")
        is True
    )
    assert (
        adapter.verify_webhook(b"", {"x-telegram-bot-api-secret-token": "wrong"}, "secret123")
        is False
    )
    assert adapter.verify_webhook(b"", {}, "secret123") is False


@pytest.mark.asyncio
async def test_poll(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    mock_get = AsyncMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ok": True,
        "result": [
            {
                "update_id": 500,
                "message": {
                    "message_id": 1,
                    "from": {"id": 1111111, "username": "polluser"},
                    "chat": {"id": 123},
                    "text": "hello",
                },
            }
        ],
    }
    mock_get.return_value = mock_response

    mock_post = AsyncMock(return_value=_telegram_api_success())

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.get = mock_get
        mock_client_instance.post = mock_post

        await adapter.initialize(channel_config, secret_resolver)

        assert adapter._offset == 0
        messages = await adapter.poll()

        assert len(messages) == 1
        assert messages[0].content == "hello"
        assert adapter._offset == 0
        await adapter.acknowledge_messages(messages)
        assert adapter._offset == 501

    mock_get.assert_called_once()
    request_url = mock_get.call_args.args[0]
    request_params = mock_get.call_args.kwargs["params"]
    assert request_url == "https://api.telegram.org/bottest-telegram-token/getUpdates"
    assert request_params["offset"] == 0
    assert request_params["timeout"] == 30
    assert json.loads(request_params["allowed_updates"]) == [
        "message",
        "message_reaction",
        "message_reaction_count",
        "callback_query",
    ]
    assert mock_get.call_args.kwargs["timeout"] == 35.0


@pytest.mark.asyncio
async def test_poll_acknowledges_only_contiguous_successful_updates(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    mock_get = AsyncMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ok": True,
        "result": [
            {
                "update_id": 500,
                "message": {
                    "message_id": 1,
                    "from": {"id": 1111111, "username": "first"},
                    "chat": {"id": 123},
                    "text": "first",
                },
            },
            {
                "update_id": 501,
                "message": {
                    "message_id": 2,
                    "from": {"id": 2222222, "username": "second"},
                    "chat": {"id": 123},
                    "text": "second",
                },
            },
        ],
    }
    mock_get.return_value = mock_response

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.get = mock_get
        mock_client_instance.post = AsyncMock(return_value=_telegram_api_success())

        await adapter.initialize(channel_config, secret_resolver)
        messages = await adapter.poll()

        await adapter.acknowledge_messages([messages[1]])
        assert adapter._offset == 0

        await adapter.acknowledge_messages([messages[0]])
        assert adapter._offset == 502


@pytest.mark.asyncio
async def test_poll_ignores_non_message_updates_for_offset_tracking(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    mock_get = AsyncMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ok": True,
        "result": [
            {"update_id": 500, "edited_message": {"message_id": 1}},
            {
                "update_id": 501,
                "message": {
                    "message_id": 2,
                    "from": {"id": 2222222, "username": "second"},
                    "chat": {"id": 123},
                    "text": "second",
                },
            },
        ],
    }
    mock_get.return_value = mock_response

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.get = mock_get
        mock_client_instance.post = AsyncMock(return_value=_telegram_api_success())

        await adapter.initialize(channel_config, secret_resolver)
        messages = await adapter.poll()

        assert [message.content for message in messages] == ["second"]

        await adapter.acknowledge_messages(messages)
        assert adapter._offset == 502


@pytest.mark.asyncio
async def test_poll_offset_restores_and_persists_after_acknowledgement(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    channel_config.config_json["poll_offset"] = 500
    persist_config = AsyncMock()
    adapter.set_config_update_callback(persist_config)

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.post = AsyncMock(return_value=_telegram_api_success())
        await adapter.initialize(channel_config, secret_resolver)

    assert adapter._offset == 500

    adapter._pending_update_ids = [500]
    message = CommsMessage(
        id="message-500",
        channel_id=channel_config.id,
        direction="inbound",
        content="hello",
        metadata_json={"telegram_update_id": 500},
        created_at=datetime.now(UTC),
    )

    await adapter.acknowledge_messages([message])

    assert adapter._offset == 501
    persist_config.assert_awaited_once_with({"poll_offset": 501})


async def test_shutdown(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    mock_aclose = AsyncMock()
    mock_post = AsyncMock(return_value=_telegram_api_success())

    with patch("httpx.AsyncClient") as MockClient:
        mock_client_instance = MockClient.return_value
        mock_client_instance.aclose = mock_aclose
        mock_client_instance.post = mock_post

        await adapter.initialize(channel_config, secret_resolver)
        adapter._message_link_preview_options[("chat", "message")] = {"is_disabled": True}
        await adapter.shutdown()

        mock_aclose.assert_called_once()
        assert adapter._client is None
        assert adapter._message_link_preview_options == {}


@pytest.mark.asyncio
async def test_send_message_honors_default_and_per_message_link_preview_options(
    adapter: TelegramAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    channel_config.config_json["link_preview_options"] = {
        "is_disabled": True,
        "show_above_text": True,
    }
    mock_post = AsyncMock()

    async def side_effect(url: str, **_kwargs: object) -> MagicMock:
        response = MagicMock()
        response.status_code = 200
        response.raise_for_status = MagicMock()
        response.json.return_value = (
            {"ok": True, "result": {"message_id": 12345}}
            if url.endswith("/sendMessage")
            else {"ok": True}
        )
        return response

    mock_post.side_effect = side_effect
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client_class.return_value.post = mock_post
        await adapter.initialize(channel_config, secret_resolver)
        mock_post.reset_mock()
        mock_post.side_effect = side_effect

        messages = [
            CommsMessage(
                id="default-preview",
                channel_id=channel_config.id,
                direction="outbound",
                content="https://example.com/default",
                metadata_json={"platform_destination": "chat999"},
                created_at=datetime.now(UTC),
            ),
            CommsMessage(
                id="override-preview",
                channel_id=channel_config.id,
                direction="outbound",
                content="https://example.com/override",
                metadata_json={
                    "platform_destination": "chat999",
                    "link_preview_options": {
                        "is_disabled": False,
                        "url": "https://example.com/override",
                    },
                },
                created_at=datetime.now(UTC),
            ),
            CommsMessage(
                id="clear-preview",
                channel_id=channel_config.id,
                direction="outbound",
                content="https://example.com/clear",
                metadata_json={
                    "platform_destination": "chat999",
                    "link_preview_options": None,
                },
                created_at=datetime.now(UTC),
            ),
        ]
        for message in messages:
            platform_message_id = await adapter.send_message(message)
            assert platform_message_id is not None
            await adapter.edit_message(
                platform_message_id,
                f"{message.content}/edited",
                "chat999",
            )

    payloads = [item.kwargs["json"] for item in mock_post.call_args_list]
    assert payloads[0]["link_preview_options"] == {
        "is_disabled": True,
        "show_above_text": True,
    }
    assert payloads[1]["link_preview_options"] == payloads[0]["link_preview_options"]
    assert payloads[2]["link_preview_options"] == {
        "is_disabled": False,
        "show_above_text": True,
        "url": "https://example.com/override",
    }
    assert payloads[3]["link_preview_options"] == payloads[2]["link_preview_options"]
    assert "link_preview_options" not in payloads[4]
    assert "link_preview_options" not in payloads[5]
