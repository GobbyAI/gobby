"""Tests for the Discord communications adapter."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from gobby.communications.adapters.discord import DiscordAdapter
from gobby.communications.models import ChannelConfig, CommsMessage

pytestmark = pytest.mark.unit


@pytest.fixture
def channel_config() -> ChannelConfig:
    return ChannelConfig(
        id="test_discord_channel",
        channel_type="discord",
        name="Test Discord",
        enabled=True,
        config_json={},
        created_at="2024-01-01T00:00:00Z",
        updated_at="2024-01-01T00:00:00Z",
    )


@pytest.fixture
def secret_resolver() -> Callable[[str], str | None]:
    def resolver(key: str) -> str | None:
        if key == "$secret:DISCORD_BOT_TOKEN":
            return "test-discord-token"
        return None

    return resolver


@pytest.fixture
def adapter() -> DiscordAdapter:
    return DiscordAdapter()


@pytest.mark.asyncio
async def test_initialize_success(
    adapter: DiscordAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    # Disable gateway so it doesn't spin up tasks in unit test
    channel_config.config_json["enable_gateway"] = False

    await adapter.initialize(channel_config, secret_resolver)

    assert adapter._bot_token == "test-discord-token"
    assert adapter._client is not None
    assert str(adapter._client.base_url) == "https://discord.com/api/v10/"


@pytest.mark.asyncio
async def test_initialize_missing_token(
    adapter: DiscordAdapter, channel_config: ChannelConfig
) -> None:
    with pytest.raises(
        ValueError, match="Could not resolve Discord bot token: \\$secret:DISCORD_BOT_TOKEN"
    ):
        await adapter.initialize(channel_config, lambda x: None)


@pytest.mark.asyncio
async def test_send_message_success(
    adapter: DiscordAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    channel_config.config_json["enable_gateway"] = False
    await adapter.initialize(channel_config, secret_resolver)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json.return_value = {"id": "1234567890"}
        mock_post.return_value = mock_response

        message = CommsMessage(
            id="msg_1",
            channel_id="gobby-internal-channel",
            direction="outbound",
            content="Hello Discord",
            created_at="2024-01-01T00:00:00Z",
            metadata_json={"platform_destination": "channel_123"},
        )

        msg_id = await adapter.send_message(message)

        assert msg_id == "1234567890"
        mock_post.assert_called_once_with(
            "/channels/channel_123/messages",
            json={"content": "Hello Discord", "allowed_mentions": {"parse": []}},
        )


def test_parse_webhook_ping(adapter: DiscordAdapter) -> None:
    payload = {"type": 1}
    messages = adapter.parse_webhook(payload, {})
    assert len(messages) == 1
    assert messages[0].content_type == "interaction_ping"
    assert json.loads(messages[0].content) == {"type": 1}


def test_parse_webhook_message(adapter: DiscordAdapter) -> None:
    payload = {
        "type": 0,
        "channel_id": "channel_123",
        "id": "msg_456",
        "author": {"id": "user_789"},
        "content": "Hello bot",
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    assert messages[0].content == "Hello bot"
    assert messages[0].identity_id == "user_789"
    assert messages[0].channel_id == ""
    assert messages[0].metadata_json["platform_channel_id"] == "channel_123"
    assert messages[0].platform_message_id == "msg_456"


def test_verify_webhook(adapter: DiscordAdapter) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    secret = public_key.public_bytes_raw().hex()

    timestamp = "1234567890"
    payload = b'{"type": 1}'
    message = timestamp.encode() + payload

    signature = private_key.sign(message)

    headers = {
        "X-Signature-Ed25519": signature.hex(),
        "X-Signature-Timestamp": timestamp,
    }

    assert adapter.verify_webhook(payload, headers, secret) is True


def test_verify_webhook_invalid_signature(adapter: DiscordAdapter) -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    secret = public_key.public_bytes_raw().hex()

    timestamp = "1234567890"
    payload = b'{"type": 1}'

    headers = {
        "X-Signature-Ed25519": "00" * 64,  # Invalid signature length is 64 bytes
        "X-Signature-Timestamp": timestamp,
    }

    assert adapter.verify_webhook(payload, headers, secret) is False


@pytest.mark.asyncio
async def test_rate_limit_headers_parsed(
    adapter: DiscordAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    """Test that REST rate limit headers are parsed and stored per route."""
    channel_config.config_json["enable_gateway"] = False
    await adapter.initialize(channel_config, secret_resolver)

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {
            "X-RateLimit-Remaining": "4",
            "X-RateLimit-Reset": "1700000000.0",
            "X-RateLimit-Bucket": "abc123",
        }
        mock_response.json.return_value = {"id": "msg1"}
        mock_post.return_value = mock_response

        message = CommsMessage(
            id="msg_1",
            channel_id="gobby-internal-channel",
            direction="outbound",
            content="Test",
            created_at="2024-01-01T00:00:00Z",
            metadata_json={"platform_destination": "channel_123"},
        )

        await adapter.send_message(message)

        route = "/channels/channel_123/messages"
        assert route in adapter._route_buckets
        assert adapter._route_buckets[route]["remaining"] == 4
        assert adapter._route_buckets[route]["bucket_id"] == "abc123"


@pytest.mark.asyncio
@patch("gobby.communications.adapters.discord.asyncio.sleep", new_callable=AsyncMock)
async def test_rate_limit_pre_wait(
    mock_sleep: AsyncMock,
    adapter: DiscordAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    """Test that exhausted route bucket triggers pre-request sleep."""
    channel_config.config_json["enable_gateway"] = False
    await adapter.initialize(channel_config, secret_resolver)

    # Pre-set an exhausted bucket
    route = "/channels/channel_123/messages"
    adapter._route_buckets[route] = {
        "remaining": 0,
        "reset": time.time() + 2.0,
        "bucket_id": "abc123",
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"X-RateLimit-Remaining": "29", "X-RateLimit-Reset": "9999999999"}
        mock_response.json.return_value = {"id": "msg1"}
        mock_post.return_value = mock_response

        message = CommsMessage(
            id="msg_1",
            channel_id="gobby-internal-channel",
            direction="outbound",
            content="Test",
            created_at="2024-01-01T00:00:00Z",
            metadata_json={"platform_destination": "channel_123"},
        )

        await adapter.send_message(message)

        # Should have slept before making the request
        assert mock_sleep.await_count == 1


def test_gateway_resume_state(adapter: DiscordAdapter) -> None:
    """Test that gateway session state is initialized for RESUME support."""
    assert adapter._session_id is None
    assert adapter._resume_gateway_url is None
    assert adapter._sequence is None


@pytest.mark.asyncio
async def test_send_identify(adapter: DiscordAdapter) -> None:
    """Test that _send_identify sends IDENTIFY (op 2)."""
    adapter._bot_token = "test-token"

    sent_messages: list[str] = []
    mock_ws = MagicMock()
    mock_ws.send = AsyncMock(side_effect=lambda data: sent_messages.append(data))

    await adapter._send_identify(mock_ws)

    assert len(sent_messages) == 1
    identify_data = json.loads(sent_messages[0])
    assert identify_data["op"] == 2
    assert identify_data["d"]["token"] == "test-token"
    assert identify_data["d"]["intents"] == 37376


@pytest.mark.asyncio
async def test_gateway_resume_logic(adapter: DiscordAdapter) -> None:
    """Test gateway URL selection when a resume URL is stored."""
    adapter._bot_token = "test-token"
    adapter._session_id = "existing-session"
    adapter._resume_gateway_url = DiscordAdapter._gateway_url_with_query("wss://resume.discord.gg")
    adapter._sequence = 42

    assert adapter._resume_gateway_url == "wss://resume.discord.gg?v=10&encoding=json"
    gateway_url = adapter._resume_gateway_url or adapter._DEFAULT_GATEWAY_URL
    assert gateway_url == "wss://resume.discord.gg?v=10&encoding=json"


@pytest.mark.asyncio
async def test_gateway_identify_when_no_session(adapter: DiscordAdapter) -> None:
    """Test that IDENTIFY is used when no prior session exists."""
    adapter._bot_token = "test-token"
    adapter._session_id = None

    # Verify gateway URL falls back to default
    gateway_url = adapter._resume_gateway_url or adapter._DEFAULT_GATEWAY_URL
    assert gateway_url == adapter._DEFAULT_GATEWAY_URL

    # Verify _send_identify works
    sent_messages: list[str] = []
    mock_ws = MagicMock()
    mock_ws.send = AsyncMock(side_effect=lambda data: sent_messages.append(data))

    await adapter._send_identify(mock_ws)

    identify_data = json.loads(sent_messages[0])
    assert identify_data["op"] == 2


@pytest.mark.asyncio
async def test_gateway_ready_stores_session(adapter: DiscordAdapter) -> None:
    """Test that READY event data fields are stored for future RESUME.

    Unit test of data assignment — _run_gateway is not easily callable in
    isolation, so we verify the expected field-level behavior directly.
    """
    assert adapter._session_id is None
    assert adapter._resume_gateway_url is None

    # Simulate what _run_gateway does on READY
    ready_data = {
        "session_id": "new-session-123",
        "resume_gateway_url": "wss://resume.discord.gg/?compress=zlib-stream",
    }
    await adapter._handle_gateway_dispatch("READY", ready_data)

    assert adapter._session_id == "new-session-123"
    assert adapter._resume_gateway_url == (
        "wss://resume.discord.gg/?compress=zlib-stream&v=10&encoding=json"
    )


@pytest.mark.asyncio
async def test_gateway_message_create_forwards_to_manager(adapter: DiscordAdapter) -> None:
    inbound_callback = AsyncMock(return_value=[])
    adapter.set_inbound_callback(inbound_callback)

    await adapter._handle_gateway_dispatch(
        "MESSAGE_CREATE",
        {
            "id": "msg_456",
            "channel_id": "channel_123",
            "author": {"id": "user_789", "username": "tester"},
            "content": "Hello from gateway",
        },
    )

    inbound_callback.assert_awaited_once()
    messages = inbound_callback.await_args.args[0]
    assert len(messages) == 1
    assert messages[0].content == "Hello from gateway"
    assert messages[0].metadata_json["platform_channel_id"] == "channel_123"


@pytest.mark.asyncio
async def test_gateway_stops_reconnecting_on_fatal_close(adapter: DiscordAdapter) -> None:
    fatal_close = ConnectionClosedError(Close(4004, "authentication failed"), None)
    connect_attempts = 0

    def raise_fatal_close(url: str) -> None:
        nonlocal connect_attempts
        connect_attempts += 1
        assert url == DiscordAdapter._DEFAULT_GATEWAY_URL
        raise fatal_close

    with (
        patch("gobby.communications.adapters.discord.HAS_WEBSOCKETS", True),
        patch(
            "gobby.communications.adapters.discord.websockets.connect",
            side_effect=raise_fatal_close,
        ),
        patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        await adapter._run_gateway()

    assert connect_attempts == 1
    mock_sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_gateway_backs_off_after_clean_close(adapter: DiscordAdapter) -> None:
    class EmptyGateway:
        send = AsyncMock()

        async def __aenter__(self) -> EmptyGateway:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def __aiter__(self) -> EmptyGateway:
            return self

        async def __anext__(self) -> str:
            raise StopAsyncIteration

    connect_attempts = 0

    def clean_connect(url: str) -> EmptyGateway:
        nonlocal connect_attempts
        connect_attempts += 1
        assert url == DiscordAdapter._DEFAULT_GATEWAY_URL
        return EmptyGateway()

    with (
        patch("gobby.communications.adapters.discord.HAS_WEBSOCKETS", True),
        patch(
            "gobby.communications.adapters.discord.websockets.connect", side_effect=clean_connect
        ),
        patch("random.uniform", return_value=0.0),
        patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        mock_sleep.side_effect = asyncio.CancelledError
        await adapter._run_gateway()

    assert connect_attempts == 1
    mock_sleep.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
async def test_heartbeat_closes_when_ack_is_missing(adapter: DiscordAdapter) -> None:
    class HeartbeatWebSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.close_kwargs: dict[str, object] | None = None

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

        async def close(self, **kwargs: object) -> None:
            self.close_kwargs = kwargs

    adapter._heartbeat_ack_received = False
    ws = HeartbeatWebSocket()

    with patch("random.random", return_value=0.0):
        await adapter._heartbeat_loop(ws, 1.0)

    assert adapter._heartbeat_ack_received is False
    assert ws.sent == []
    assert ws.close_kwargs == {"code": 4000, "reason": "Heartbeat ACK timeout"}


@pytest.mark.asyncio
async def test_invalid_session_clears_state(adapter: DiscordAdapter) -> None:
    """Non-resumable Invalid Session (op 9, d=false) clears state and IDENTIFYs."""
    adapter._bot_token = "test-token"
    adapter._session_id = "old-session"
    adapter._resume_gateway_url = "wss://resume.discord.gg"
    adapter._sequence = 10

    sent: list[str] = []

    class InvalidSessionGateway:
        def __init__(self) -> None:
            self._messages = [json.dumps({"op": 9, "d": False})]

        async def send(self, payload: str) -> None:
            sent.append(payload)

        async def __aenter__(self) -> InvalidSessionGateway:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def __aiter__(self) -> InvalidSessionGateway:
            return self

        async def __anext__(self) -> str:
            if self._messages:
                return self._messages.pop(0)
            raise StopAsyncIteration

    sleep_calls = 0

    async def fake_sleep(_delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    with (
        patch("gobby.communications.adapters.discord.HAS_WEBSOCKETS", True),
        patch(
            "gobby.communications.adapters.discord.websockets.connect",
            return_value=InvalidSessionGateway(),
        ),
        patch("random.random", return_value=0.0),
        patch("random.uniform", return_value=0.0),
        patch("asyncio.sleep", side_effect=fake_sleep),
    ):
        await adapter._run_gateway()

    assert adapter._session_id is None
    assert adapter._resume_gateway_url is None
    assert adapter._sequence is None
    identify_payloads = [
        json.loads(payload) for payload in sent if json.loads(payload).get("op") == 2
    ]
    assert identify_payloads, sent


# --- Embed support ---


@pytest.mark.asyncio
async def test_send_message_embed(
    adapter: DiscordAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    """send_message with content_type='embed' sends embeds array in payload."""
    channel_config.config_json["enable_gateway"] = False
    await adapter.initialize(channel_config, secret_resolver)

    embed = json.dumps({"title": "Test", "description": "Hello embed"})
    message = CommsMessage(
        id="msg_embed",
        channel_id="gobby-internal-channel",
        direction="outbound",
        content=embed,
        created_at="2024-01-01T00:00:00Z",
        content_type="embed",
        metadata_json={"fallback_text": "Test fallback", "platform_destination": "channel_123"},
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json.return_value = {"id": "embed_msg_1"}
        mock_post.return_value = mock_response

        msg_id = await adapter.send_message(message)

    assert msg_id == "embed_msg_1"
    call_kwargs = mock_post.call_args[1]["json"]
    assert call_kwargs["embeds"] == [{"title": "Test", "description": "Hello embed"}]
    assert call_kwargs["content"] == "Test fallback"
    assert call_kwargs["allowed_mentions"] == {"parse": []}


@pytest.mark.asyncio
async def test_send_message_embed_list(
    adapter: DiscordAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    """Embed list passed directly without wrapping."""
    channel_config.config_json["enable_gateway"] = False
    await adapter.initialize(channel_config, secret_resolver)

    embeds = json.dumps([{"title": "Embed 1"}, {"title": "Embed 2"}])
    message = CommsMessage(
        id="msg_embeds",
        channel_id="gobby-internal-channel",
        direction="outbound",
        content=embeds,
        created_at="2024-01-01T00:00:00Z",
        content_type="embed",
        metadata_json={"platform_destination": "channel_123"},
    )

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json.return_value = {"id": "embed_msg_2"}
        mock_post.return_value = mock_response

        msg_id = await adapter.send_message(message)

    assert msg_id == "embed_msg_2"
    call_kwargs = mock_post.call_args[1]["json"]
    assert len(call_kwargs["embeds"]) == 2
    assert call_kwargs["allowed_mentions"] == {"parse": []}


@pytest.mark.asyncio
async def test_send_message_embed_invalid_json(
    adapter: DiscordAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    """Malformed embed JSON raises ValueError."""
    channel_config.config_json["enable_gateway"] = False
    await adapter.initialize(channel_config, secret_resolver)

    message = CommsMessage(
        id="msg_bad",
        channel_id="gobby-internal-channel",
        direction="outbound",
        content="not json",
        created_at="2024-01-01T00:00:00Z",
        content_type="embed",
        metadata_json={"platform_destination": "channel_123"},
    )

    with pytest.raises(ValueError, match="Invalid embed JSON"):
        await adapter.send_message(message)


@pytest.mark.asyncio
async def test_send_message_embed_title_too_long(
    adapter: DiscordAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    """Embed with title > 256 chars raises ValueError."""
    channel_config.config_json["enable_gateway"] = False
    await adapter.initialize(channel_config, secret_resolver)

    embed = json.dumps({"title": "x" * 257})
    message = CommsMessage(
        id="msg_long_title",
        channel_id="gobby-internal-channel",
        direction="outbound",
        content=embed,
        created_at="2024-01-01T00:00:00Z",
        content_type="embed",
        metadata_json={"platform_destination": "channel_123"},
    )

    with pytest.raises(ValueError, match="title exceeds 256 chars"):
        await adapter.send_message(message)


@pytest.mark.asyncio
async def test_send_message_embed_too_many_fields(
    adapter: DiscordAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    """Embed with > 25 fields raises ValueError."""
    channel_config.config_json["enable_gateway"] = False
    await adapter.initialize(channel_config, secret_resolver)

    embed = json.dumps({"fields": [{"name": f"f{i}", "value": "v"} for i in range(26)]})
    message = CommsMessage(
        id="msg_fields",
        channel_id="gobby-internal-channel",
        direction="outbound",
        content=embed,
        created_at="2024-01-01T00:00:00Z",
        content_type="embed",
        metadata_json={"platform_destination": "channel_123"},
    )

    with pytest.raises(ValueError, match="26 fields"):
        await adapter.send_message(message)


# --- Dynamic gateway URL ---


@pytest.mark.asyncio
async def test_fetch_gateway_url_success(
    adapter: DiscordAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    """initialize() fetches gateway URL from GET /gateway/bot."""
    channel_config.config_json["enable_gateway"] = False

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "url": "wss://gateway.discord.gg?compress=zlib-stream",
            "shards": 1,
            "session_start_limit": {
                "total": 1000,
                "remaining": 995,
                "reset_after": 14400000,
            },
        }
        mock_get.return_value = mock_response

        await adapter.initialize(channel_config, secret_resolver)

    assert (
        adapter._gateway_url == "wss://gateway.discord.gg?compress=zlib-stream&v=10&encoding=json"
    )


@pytest.mark.asyncio
async def test_fetch_gateway_url_fallback_on_error(
    adapter: DiscordAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    """Falls back to default URL when API fails."""
    channel_config.config_json["enable_gateway"] = False

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.side_effect = httpx.ConnectError("connection refused")

        await adapter.initialize(channel_config, secret_resolver)

    assert adapter._gateway_url == DiscordAdapter._DEFAULT_GATEWAY_URL


@pytest.mark.asyncio
async def test_fetch_gateway_url_fallback_on_http_error(
    adapter: DiscordAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    """Falls back to default URL when API returns non-200."""
    channel_config.config_json["enable_gateway"] = False

    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_get.return_value = mock_response

        await adapter.initialize(channel_config, secret_resolver)

    assert adapter._gateway_url == DiscordAdapter._DEFAULT_GATEWAY_URL


@pytest.mark.asyncio
async def test_send_message_chunking(
    adapter: DiscordAdapter,
    channel_config: ChannelConfig,
    secret_resolver: Callable[[str], str | None],
) -> None:
    channel_config.config_json["enable_gateway"] = False
    await adapter.initialize(channel_config, secret_resolver)

    long_content = "A" * 2500
    msg = CommsMessage(
        id="test_id",
        channel_id="gobby-internal-channel",
        direction="outbound",
        content=long_content,
        metadata_json={"platform_destination": "channel_123"},
        created_at="2024-01-01T00:00:00Z",
    )

    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json.return_value = {"id": "msg_456"}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        result = await adapter.send_message(msg)

        assert mock_post.call_count == 2
        assert result == "msg_456"


def test_parse_webhook_reaction_added(adapter: DiscordAdapter) -> None:
    """parse_webhook() parses MESSAGE_REACTION_ADD into reaction CommsMessage."""
    payload = {
        "t": "MESSAGE_REACTION_ADD",
        "d": {
            "user_id": "user_789",
            "channel_id": "channel_123",
            "message_id": "msg_456",
            "emoji": {"id": None, "name": "thumbsup"},
        },
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    msg = messages[0]
    assert msg.content_type == "reaction"
    assert msg.content == "thumbsup"
    assert msg.platform_message_id == "msg_456"
    assert msg.identity_id == "user_789"
    assert msg.channel_id == ""
    assert msg.metadata_json["platform_channel_id"] == "channel_123"


def test_parse_webhook_extracts_thread_id(adapter: DiscordAdapter) -> None:
    """parse_webhook() extracts platform_thread_id from thread metadata."""
    payload = {
        "type": 0,
        "channel_id": "channel_123",
        "id": "msg_123",
        "author": {"id": "user_123"},
        "content": "Reply in thread",
        "thread": {"id": "thread_999"},
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    assert messages[0].platform_thread_id == "thread_999"


def test_parse_webhook_extracts_thread_from_message_reference(adapter: DiscordAdapter) -> None:
    """parse_webhook() extracts thread from message_reference when no thread metadata."""
    payload = {
        "type": 0,
        "channel_id": "channel_123",
        "id": "msg_123",
        "author": {"id": "user_123"},
        "content": "Reply via reference",
        "message_reference": {"channel_id": "thread_888", "message_id": "msg_original"},
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    assert messages[0].platform_thread_id == "thread_888"
