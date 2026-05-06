"""Tests for the Teams communications adapter."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest

from gobby.communications.adapters.teams import TeamsAdapter
from gobby.communications.models import ChannelConfig, CommsMessage


@pytest.fixture
def channel_config() -> ChannelConfig:
    return ChannelConfig(
        id="test_channel",
        channel_type="teams",
        name="Test Teams",
        enabled=True,
        config_json={},
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )


@pytest.fixture
def secret_resolver() -> Any:
    def resolver(key: str) -> str | None:
        if key == "$secret:TEAMS_APP_ID":
            return "test-app-id"
        if key == "$secret:TEAMS_APP_PASSWORD":
            return "test-app-password"
        return None

    return resolver


@pytest.fixture
def adapter() -> TeamsAdapter:
    return TeamsAdapter()


@pytest.mark.asyncio
async def test_initialize_success(
    adapter: TeamsAdapter, channel_config: ChannelConfig, secret_resolver: Any
) -> None:
    """Test successful initialization."""
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "test-token", "expires_in": 3600}
        mock_client.post.return_value = mock_response
        mock_client_class.return_value = mock_client

        await adapter.initialize(channel_config, secret_resolver)

        assert adapter._app_id == "test-app-id"
        assert adapter._app_password == "test-app-password"
        assert adapter._access_token == "test-token"
        assert adapter._client is not None
        mock_client.post.assert_called_once()


@pytest.mark.asyncio
async def test_initialize_missing_secrets(
    adapter: TeamsAdapter, channel_config: ChannelConfig
) -> None:
    """Test initialization with missing secrets."""

    def empty_resolver(key: str) -> str | None:
        return None

    with pytest.raises(
        ValueError, match="TEAMS_APP_ID and TEAMS_APP_PASSWORD secrets are required"
    ):
        await adapter.initialize(channel_config, empty_resolver)


@pytest.mark.asyncio
async def test_send_message_success(
    adapter: TeamsAdapter, channel_config: ChannelConfig, secret_resolver: Any
) -> None:
    """Test sending a text message."""
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_auth_resp = MagicMock()
        mock_auth_resp.json.return_value = {"access_token": "test-token", "expires_in": 3600}
        mock_client.post.return_value = mock_auth_resp
        mock_client_class.return_value = mock_client

        await adapter.initialize(channel_config, secret_resolver)

        # Mock the send message response
        mock_send_resp = MagicMock()
        mock_send_resp.status_code = 200
        mock_send_resp.json.return_value = {"id": "msg-123"}
        mock_client.post.return_value = mock_send_resp

        message = CommsMessage(
            id="1",
            channel_id="gobby-internal-uuid",
            direction="outbound",
            content="Hello world",
            metadata_json={
                "service_url": "https://smba.trafficmanager.net/apis/",
                "platform_destination": "conv-1",
            },
            created_at="2024-01-01T00:00:00Z",
        )

        msg_id = await adapter.send_message(message)

        assert msg_id == "msg-123"
        assert mock_client.post.call_args.kwargs["json"]["text"] == "Hello world"


@pytest.mark.asyncio
async def test_send_message_adaptive_card(
    adapter: TeamsAdapter, channel_config: ChannelConfig, secret_resolver: Any
) -> None:
    """Test sending an adaptive card message."""
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_auth_resp = MagicMock()
        mock_auth_resp.json.return_value = {"access_token": "test-token", "expires_in": 3600}
        mock_client.post.return_value = mock_auth_resp
        mock_client_class.return_value = mock_client

        await adapter.initialize(channel_config, secret_resolver)

        # Mock the send message response
        mock_send_resp = MagicMock()
        mock_send_resp.status_code = 200
        mock_send_resp.json.return_value = {"id": "msg-123"}
        mock_client.post.return_value = mock_send_resp

        card_json = '{"type": "AdaptiveCard", "version": "1.0", "body": []}'
        message = CommsMessage(
            id="1",
            channel_id="conv-1",
            direction="outbound",
            content=card_json,
            content_type="adaptive_card",
            metadata_json={"service_url": "https://smba.trafficmanager.net/apis/"},
            created_at="2024-01-01T00:00:00Z",
        )

        await adapter.send_message(message)

        expected_json = {
            "type": "message",
            "text": "",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {"type": "AdaptiveCard", "version": "1.0", "body": []},
                }
            ],
        }

        mock_client.post.assert_called_with(
            "https://smba.trafficmanager.net/apis/v3/conversations/conv-1/activities",
            json=expected_json,
            headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        )
        assert mock_client.post.call_count >= 1
        assert mock_client.post.call_args is not None


def test_parse_webhook(adapter: TeamsAdapter) -> None:
    """Test parsing a Bot Framework webhook payload."""
    payload = {
        "type": "message",
        "id": "msg-456",
        "text": "Hello bot",
        "from": {"id": "user-789", "name": "Test User"},
        "conversation": {"id": "conv-123"},
        "serviceUrl": "https://smba.trafficmanager.net/apis/",
        "replyToId": "msg-111",
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    msg = messages[0]
    assert msg.content == "Hello bot"
    assert msg.channel_id == "conv-123"
    assert msg.identity_id == "user-789"
    assert msg.platform_message_id == "msg-456"
    assert msg.platform_thread_id == "msg-111"
    assert msg.metadata_json["service_url"] == "https://smba.trafficmanager.net/apis/"


def test_verify_webhook_success(adapter: TeamsAdapter) -> None:
    """Test webhook verification success with mocked JWKS."""
    adapter._app_id = "test-app-id"

    mock_jwk_client = MagicMock()
    mock_signing_key = MagicMock()
    mock_signing_key.key = "test-rsa-key"
    mock_jwk_client.get_signing_key_from_jwt.return_value = mock_signing_key

    token = jwt.encode(
        {"aud": "test-app-id", "iss": "https://api.botframework.com"},
        "test-secret-key-that-is-at-least-32-bytes-long",
        algorithm="HS256",
    )
    headers = {"Authorization": f"Bearer {token}"}

    adapter._jwk_client = mock_jwk_client
    with patch("jwt.decode") as mock_decode:
        mock_decode.return_value = {
            "aud": "test-app-id",
            "iss": "https://api.botframework.com",
        }
        assert adapter.verify_webhook(b"", headers, "not-used") is True

        mock_decode.assert_called_once_with(
            token,
            "test-rsa-key",
            algorithms=["RS256"],
            audience="test-app-id",
            issuer="https://api.botframework.com",
        )


def test_verify_webhook_failure(adapter: TeamsAdapter) -> None:
    """Test webhook verification failures."""
    adapter._app_id = "test-app-id"

    # Missing auth header
    assert adapter.verify_webhook(b"", {}, "not-used") is False

    # JWT verification error (invalid signature, wrong audience, wrong issuer)
    mock_jwk_client = MagicMock()
    mock_signing_key = MagicMock()
    mock_signing_key.key = "test-rsa-key"
    mock_jwk_client.get_signing_key_from_jwt.return_value = mock_signing_key

    token = jwt.encode(
        {"aud": "test-app-id", "iss": "https://api.botframework.com"},
        "test-secret-key-that-is-at-least-32-bytes-long",
        algorithm="HS256",
    )

    adapter._jwk_client = mock_jwk_client
    with patch("jwt.decode") as mock_decode:
        mock_decode.side_effect = jwt.InvalidTokenError("bad signature")
        assert (
            adapter.verify_webhook(b"", {"Authorization": f"Bearer {token}"}, "not-used") is False
        )

    # JWKS fetch failure
    mock_bad_client = MagicMock()
    mock_bad_client.get_signing_key_from_jwt.side_effect = jwt.PyJWKClientError("fetch failed")
    adapter._jwk_client = mock_bad_client
    assert adapter.verify_webhook(b"", {"Authorization": f"Bearer {token}"}, "not-used") is False


def test_capabilities(adapter: TeamsAdapter) -> None:
    """Test adapter capabilities."""
    caps = adapter.capabilities()
    assert caps.threading is True
    assert caps.reactions is False
    assert caps.files is True
    assert caps.markdown is True
    assert caps.max_message_length == 28000


def test_properties(adapter: TeamsAdapter) -> None:
    """Test adapter properties."""
    assert adapter.channel_type == "teams"
    assert adapter.max_message_length == 28000
    assert adapter.supports_webhooks is True
    assert adapter.supports_polling is False


@pytest.mark.asyncio
async def test_token_refresh_lock(
    adapter: TeamsAdapter, channel_config: ChannelConfig, secret_resolver: Any
) -> None:
    """Test that concurrent send_message calls only refresh the token once."""
    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_auth_resp = MagicMock()
        mock_auth_resp.json.return_value = {"access_token": "test-token", "expires_in": 3600}
        mock_client.post.return_value = mock_auth_resp
        mock_client_class.return_value = mock_client

        await adapter.initialize(channel_config, secret_resolver)

        # Force token to be expired
        adapter._token_expires_at = 0

        refresh_call_count = 0
        original_refresh = adapter._refresh_token

        async def counting_refresh() -> None:
            nonlocal refresh_call_count
            refresh_call_count += 1
            await original_refresh()

        adapter._refresh_token = counting_refresh  # type: ignore[assignment]

        # Mock send response
        mock_send_resp = MagicMock()
        mock_send_resp.status_code = 200
        mock_send_resp.json.return_value = {"id": "msg-1"}
        mock_client.post.return_value = mock_send_resp

        message = CommsMessage(
            id="1",
            channel_id="conv-1",
            direction="outbound",
            content="Hello",
            metadata_json={"service_url": "https://smba.trafficmanager.net/apis/"},
            created_at="2024-01-01T00:00:00Z",
        )

        # Fire two concurrent sends — only one should trigger refresh
        await asyncio.gather(
            adapter.send_message(message),
            adapter.send_message(message),
        )

        assert refresh_call_count == 1
        assert mock_client.post.call_count == 4


# --- Proactive messaging ---


def test_parse_webhook_stores_conversation_reference(adapter: TeamsAdapter) -> None:
    """parse_webhook stores ConversationReference in metadata and cache."""
    payload = {
        "type": "message",
        "id": "msg-100",
        "text": "Hi",
        "from": {"id": "user-1"},
        "recipient": {"id": "bot-1"},
        "conversation": {"id": "conv-abc", "tenantId": "tenant-xyz"},
        "serviceUrl": "https://smba.trafficmanager.net/apis/",
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    conv_ref = messages[0].metadata_json["conversation_reference"]
    assert conv_ref["service_url"] == "https://smba.trafficmanager.net/apis/"
    assert conv_ref["conversation_id"] == "conv-abc"
    assert conv_ref["tenant_id"] == "tenant-xyz"
    assert conv_ref["bot_id"] == "bot-1"
    # Also cached on adapter
    assert "conv-abc" in adapter._conversation_refs


@pytest.mark.asyncio
async def test_send_proactive_success(adapter: TeamsAdapter) -> None:
    """send_proactive sends using stored ConversationReference."""
    adapter._client = AsyncMock()
    adapter._access_token = "test-token"
    adapter._token_expires_at = time.time() + 3600

    # Store a conversation reference
    adapter._conversation_refs["conv-abc"] = {
        "service_url": "https://smba.trafficmanager.net/apis/",
        "conversation_id": "conv-abc",
        "tenant_id": "tenant-xyz",
        "bot_id": "bot-1",
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "proactive-msg-1"}
    adapter._client.post.return_value = mock_response

    msg_id = await adapter.send_proactive("conv-abc", "Hello proactively!")

    assert msg_id == "proactive-msg-1"
    adapter._client.post.assert_called_once()
    call_kwargs = adapter._client.post.call_args
    assert "conv-abc/activities" in call_kwargs[0][0]
    assert call_kwargs[1]["json"]["text"] == "Hello proactively!"


@pytest.mark.asyncio
async def test_send_proactive_no_reference(adapter: TeamsAdapter) -> None:
    """send_proactive raises ValueError when no ConversationReference stored."""
    adapter._client = AsyncMock()
    adapter._access_token = "test-token"
    adapter._token_expires_at = time.time() + 3600

    with pytest.raises(ValueError, match="No ConversationReference stored"):
        await adapter.send_proactive("unknown-conv", "Hello")
