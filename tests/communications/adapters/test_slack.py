"""Tests for the Slack communications adapter."""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.communications.adapters.slack import SlackAdapter, SlackVerificationChallenge
from gobby.communications.models import ChannelConfig, CommsAttachment, CommsMessage


@pytest.fixture
def channel_config() -> ChannelConfig:
    return ChannelConfig(
        id="test_channel",
        channel_type="slack",
        name="Test Slack",
        enabled=True,
        config_json={},
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )


@pytest.fixture
def secret_resolver() -> Any:
    def resolver(key: str) -> str | None:
        if key == "$secret:SLACK_BOT_TOKEN":
            return "xoxb-test-token"
        if key == "$secret:SLACK_SIGNING_SECRET":
            return "test-signing-secret"
        return None

    return resolver


@pytest.fixture
def adapter() -> SlackAdapter:
    return SlackAdapter()


@pytest.mark.asyncio
async def test_initialize_success(
    adapter: SlackAdapter, channel_config: ChannelConfig, secret_resolver: Any
) -> None:
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "user_id": "U12345"}
        mock_post.return_value = mock_response

        await adapter.initialize(channel_config, secret_resolver)

        assert adapter._bot_token == "xoxb-test-token"
        assert adapter._signing_secret == "test-signing-secret"
        assert adapter._bot_user_id == "U12345"
        mock_post.assert_called_once_with("auth.test")


@pytest.mark.asyncio
async def test_initialize_missing_token(
    adapter: SlackAdapter, channel_config: ChannelConfig
) -> None:
    with pytest.raises(ValueError, match="SLACK_BOT_TOKEN secret is required"):
        await adapter.initialize(channel_config, lambda x: None)


@pytest.mark.asyncio
async def test_initialize_auth_failure(
    adapter: SlackAdapter, channel_config: ChannelConfig, secret_resolver: Any
) -> None:
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": False, "error": "invalid_auth"}
        mock_post.return_value = mock_response

        with pytest.raises(ValueError, match="Slack auth.test failed: invalid_auth"):
            await adapter.initialize(channel_config, secret_resolver)


@pytest.mark.asyncio
async def test_send_message_success(
    adapter: SlackAdapter, channel_config: ChannelConfig, secret_resolver: Any
) -> None:
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_auth_response = MagicMock()
        mock_auth_response.json.return_value = {"ok": True, "user_id": "U12345"}
        mock_post.return_value = mock_auth_response
        await adapter.initialize(channel_config, secret_resolver)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"ok": True, "ts": "1234567890.123456"}
        mock_post.return_value = mock_response

        message = CommsMessage(
            id="msg_1",
            channel_id="C12345",
            direction="outbound",
            content="Hello World",
            created_at="2024-01-01T00:00:00Z",
            platform_thread_id="thread_123",
        )

        ts = await adapter.send_message(message)

        assert ts == "1234567890.123456"
        mock_post.assert_called_once_with(
            "chat.postMessage",
            json={
                "channel": "C12345",
                "text": "Hello World",
                "thread_ts": "thread_123",
            },
        )
        assert mock_post.call_count == 1
        assert mock_post.call_args is not None


@pytest.mark.asyncio
async def test_send_attachment_success(
    adapter: SlackAdapter, channel_config: ChannelConfig, secret_resolver: Any, tmp_path: Any
) -> None:
    # 1. Initialize
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_auth_response = MagicMock()
        mock_auth_response.json.return_value = {"ok": True, "user_id": "U12345"}
        mock_post.return_value = mock_auth_response
        await adapter.initialize(channel_config, secret_resolver)

    # 2. Setup file
    test_file = tmp_path / "test.txt"
    test_file.write_text("hello world")
    message = CommsMessage(
        id="msg_1",
        channel_id="C12345",
        direction="outbound",
        content="Check this out",
        created_at="2024-01-01T00:00:00Z",
    )
    attachment = MagicMock()
    attachment.filename = "test.txt"
    attachment.content_type = "text/plain"

    # 3. Mock the 3-step flow
    with (
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
        patch("httpx.AsyncClient.put", new_callable=AsyncMock) as mock_put,
    ):
        # Step 1 response
        mock_get_url_resp = MagicMock()
        mock_get_url_resp.status_code = 200
        mock_get_url_resp.json.return_value = {
            "ok": True,
            "upload_url": "https://upload.slack.com/123",
            "file_id": "F123",
        }

        # Step 3 response
        mock_complete_resp = MagicMock()
        mock_complete_resp.status_code = 200
        mock_complete_resp.json.return_value = {
            "ok": True,
            "files": [
                {"id": "F123", "shares": {"public": {"C12345": [{"ts": "1234567890.123456"}]}}}
            ],
        }

        mock_post.side_effect = [mock_get_url_resp, mock_complete_resp]

        # Step 2 response (PUT)
        mock_put_resp = MagicMock()
        mock_put_resp.status_code = 200
        mock_put.return_value = mock_put_resp

        ts = await adapter.send_attachment(message, attachment, test_file)

        assert ts == "1234567890.123456"

        # Verify Step 1
        mock_post.assert_any_call(
            "files.getUploadURLExternal", data={"filename": "test.txt", "length": 11}
        )
        assert mock_post.call_count >= 1
        assert mock_post.call_args is not None

        # Verify Step 2
        mock_put.assert_called_once_with(
            "https://upload.slack.com/123",
            content=b"hello world",
            headers={"Content-Type": "text/plain"},
        )
        assert mock_put.call_count == 1
        assert mock_put.call_args is not None

        # Verify Step 3
        mock_post.assert_any_call(
            "files.completeUploadExternal",
            data={
                "files": '[{"id": "F123", "title": "test.txt"}]',
                "channel_id": "C12345",
                "initial_comment": "Check this out",
            },
        )
        assert mock_post.call_count >= 1
        assert mock_post.call_args is not None


def test_parse_webhook_url_verification(adapter: SlackAdapter) -> None:
    payload = {"type": "url_verification", "challenge": "test_challenge"}
    with pytest.raises(SlackVerificationChallenge) as exc_info:
        adapter.parse_webhook(payload, {})
    assert exc_info.value.challenge == "test_challenge"


def test_parse_webhook_event_callback(adapter: SlackAdapter) -> None:
    payload = {
        "type": "event_callback",
        "event": {
            "type": "message",
            "text": "Hello bot",
            "user": "U123",
            "channel": "C123",
            "ts": "123.456",
            "thread_ts": "thread.789",
        },
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    assert messages[0].content == "Hello bot"
    assert messages[0].identity_id == "U123"
    assert messages[0].channel_id == "C123"
    assert messages[0].platform_message_id == "123.456"
    assert messages[0].platform_thread_id == "thread.789"


def test_verify_webhook(adapter: SlackAdapter) -> None:
    secret = "test_secret"
    timestamp = str(int(time.time()))
    payload = b'{"test": "data"}'

    sig_basestring = f"v0:{timestamp}:{payload.decode('utf-8')}"
    signature = (
        "v0="
        + hmac.new(
            secret.encode("utf-8"), sig_basestring.encode("utf-8"), hashlib.sha256
        ).hexdigest()
    )

    headers = {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": signature,
    }

    assert adapter.verify_webhook(payload, headers, secret) is True


def test_verify_webhook_invalid_signature(adapter: SlackAdapter) -> None:
    secret = "test_secret"
    timestamp = str(int(time.time()))
    payload = b'{"test": "data"}'

    headers = {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": "v0=invalid_signature",
    }

    assert adapter.verify_webhook(payload, headers, secret) is False


def test_verify_webhook_replay_attack(adapter: SlackAdapter) -> None:
    secret = "test_secret"
    # Old timestamp (more than 5 minutes ago)
    timestamp = str(int(time.time()) - 400)
    payload = b'{"test": "data"}'

    sig_basestring = f"v0:{timestamp}:{payload.decode('utf-8')}"
    signature = (
        "v0="
        + hmac.new(
            secret.encode("utf-8"), sig_basestring.encode("utf-8"), hashlib.sha256
        ).hexdigest()
    )

    headers = {
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": signature,
    }

    assert adapter.verify_webhook(payload, headers, secret) is False


@pytest.mark.asyncio
async def test_send_attachment_three_step_upload(
    adapter: SlackAdapter, channel_config: ChannelConfig, secret_resolver: Any
) -> None:
    """Test the files.getUploadURLExternal 3-step upload flow."""
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_auth_response = MagicMock()
        mock_auth_response.json.return_value = {"ok": True, "user_id": "U12345"}
        mock_post.return_value = mock_auth_response
        await adapter.initialize(channel_config, secret_resolver)

    with (
        patch("asyncio.to_thread", new_callable=AsyncMock) as mock_to_thread,
        patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post,
        patch("httpx.AsyncClient.put", new_callable=AsyncMock) as mock_put,
    ):
        mock_to_thread.return_value = b"file-content-bytes"

        # Step 1: getUploadURLExternal response
        mock_get_url_resp = MagicMock()
        mock_get_url_resp.status_code = 200
        mock_get_url_resp.json.return_value = {
            "ok": True,
            "upload_url": "https://files.slack.com/upload/v1/abc123",
            "file_id": "F123456",
        }

        # Step 3: completeUploadExternal response
        mock_complete_resp = MagicMock()
        mock_complete_resp.status_code = 200
        mock_complete_resp.json.return_value = {
            "ok": True,
            "files": [
                {
                    "id": "F123456",
                    "shares": {"public": {"C12345": [{"ts": "111.222"}]}},
                }
            ],
        }

        mock_post.side_effect = [mock_get_url_resp, mock_complete_resp]

        # Step 2: PUT to upload_url
        mock_upload_resp = MagicMock()
        mock_upload_resp.status_code = 200
        mock_upload_resp.raise_for_status = MagicMock()
        mock_put.return_value = mock_upload_resp

        message = CommsMessage(
            id="msg_1",
            channel_id="C12345",
            direction="outbound",
            content="Here's the file",
            created_at="2024-01-01T00:00:00Z",
            platform_thread_id="thread_1",
        )

        attachment = CommsAttachment(
            id="att_1",
            message_id="msg_1",
            filename="test.txt",
            content_type="text/plain",
            size_bytes=18,
        )

        ts = await adapter.send_attachment(message, attachment, Path("/fake/test.txt"))

        assert ts == "111.222"

        # Verify step 1: getUploadURLExternal
        step1_call = mock_post.call_args_list[0]
        assert step1_call[0][0] == "files.getUploadURLExternal"

        # Verify step 3: completeUploadExternal
        step3_call = mock_post.call_args_list[1]
        assert step3_call[0][0] == "files.completeUploadExternal"
        step3_data = step3_call[1]["data"]
        assert step3_data["channel_id"] == "C12345"
        assert step3_data["thread_ts"] == "thread_1"
        assert step3_data["initial_comment"] == "Here's the file"


# --- Block Kit support ---


@pytest.mark.asyncio
async def test_send_message_blocks(
    adapter: SlackAdapter, channel_config: ChannelConfig, secret_resolver: Any
) -> None:
    """send_message with content_type='blocks' sends blocks array in payload."""
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = MagicMock(json=lambda: {"ok": True, "user_id": "U12345"})
        await adapter.initialize(channel_config, secret_resolver)

    blocks = '[{"type": "section", "text": {"type": "mrkdwn", "text": "Hello"}}]'
    message = CommsMessage(
        id="msg_blocks",
        channel_id="C12345",
        direction="outbound",
        content=blocks,
        created_at="2024-01-01T00:00:00Z",
        content_type="blocks",
        metadata_json={"fallback_text": "Hello fallback"},
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200, json=lambda: {"ok": True, "ts": "123.456"}
        )
        ts = await adapter.send_message(message)

    assert ts == "123.456"
    call_kwargs = mock_post.call_args[1]["json"]
    assert call_kwargs["blocks"] == [
        {"type": "section", "text": {"type": "mrkdwn", "text": "Hello"}}
    ]
    assert call_kwargs["text"] == "Hello fallback"


@pytest.mark.asyncio
async def test_send_message_markdown(
    adapter: SlackAdapter, channel_config: ChannelConfig, secret_resolver: Any
) -> None:
    """send_message with content_type='markdown' wraps in mrkdwn section block."""
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = MagicMock(json=lambda: {"ok": True, "user_id": "U12345"})
        await adapter.initialize(channel_config, secret_resolver)

    message = CommsMessage(
        id="msg_md",
        channel_id="C12345",
        direction="outbound",
        content="*bold* and _italic_",
        created_at="2024-01-01T00:00:00Z",
        content_type="markdown",
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200, json=lambda: {"ok": True, "ts": "789.012"}
        )
        ts = await adapter.send_message(message)

    assert ts == "789.012"
    call_kwargs = mock_post.call_args[1]["json"]
    assert call_kwargs["blocks"] == [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*bold* and _italic_"}}
    ]
    assert call_kwargs["text"] == "*bold* and _italic_"


@pytest.mark.asyncio
async def test_send_message_blocks_invalid_json(
    adapter: SlackAdapter, channel_config: ChannelConfig, secret_resolver: Any
) -> None:
    """Malformed blocks JSON raises ValueError."""
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = MagicMock(json=lambda: {"ok": True, "user_id": "U12345"})
        await adapter.initialize(channel_config, secret_resolver)

    message = CommsMessage(
        id="msg_bad",
        channel_id="C12345",
        direction="outbound",
        content="not json",
        created_at="2024-01-01T00:00:00Z",
        content_type="blocks",
    )

    with pytest.raises(ValueError, match="Invalid Block Kit JSON"):
        await adapter.send_message(message)


@pytest.mark.asyncio
async def test_send_message_blocks_not_list(
    adapter: SlackAdapter, channel_config: ChannelConfig, secret_resolver: Any
) -> None:
    """Blocks content that is not a list raises ValueError."""
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = MagicMock(json=lambda: {"ok": True, "user_id": "U12345"})
        await adapter.initialize(channel_config, secret_resolver)

    message = CommsMessage(
        id="msg_obj",
        channel_id="C12345",
        direction="outbound",
        content='{"type": "section"}',
        created_at="2024-01-01T00:00:00Z",
        content_type="blocks",
    )

    with pytest.raises(ValueError, match="must be a JSON array"):
        await adapter.send_message(message)


@pytest.mark.asyncio
async def test_send_message_blocks_missing_type(
    adapter: SlackAdapter, channel_config: ChannelConfig, secret_resolver: Any
) -> None:
    """Block without type field raises ValueError."""
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = MagicMock(json=lambda: {"ok": True, "user_id": "U12345"})
        await adapter.initialize(channel_config, secret_resolver)

    message = CommsMessage(
        id="msg_notype",
        channel_id="C12345",
        direction="outbound",
        content='[{"text": "no type field"}]',
        created_at="2024-01-01T00:00:00Z",
        content_type="blocks",
    )

    with pytest.raises(ValueError, match="must be a dict with a 'type' field"):
        await adapter.send_message(message)
