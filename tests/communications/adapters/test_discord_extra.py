"""Extra tests for gobby.communications.adapters.discord."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

try:
    from gobby.communications.adapters.discord import DiscordAdapter
except ImportError:
    DiscordAdapter = None


def create_mock_message(**kwargs: Any) -> MagicMock:
    """Helper to mock CommsMessage without importing it (as it needs DB overhead setup or just pure mock)."""
    msg = MagicMock()
    msg.content = "content"
    msg.channel_id = "chan_1"
    msg.metadata_json = {"platform_destination": "chan_1"}
    msg.platform_thread_id = None
    for k, v in kwargs.items():
        setattr(msg, k, v)
    return msg


@pytest.fixture
def adapter() -> Any:
    if not DiscordAdapter:
        pytest.skip("Discord adapter unavailable")
    a = DiscordAdapter()
    a._bot_token = "fake-token"
    a._client = AsyncMock()
    return a


class TestDiscordExtras:
    @pytest.mark.asyncio
    async def test_raise_on_uninitialized_client(self):
        """Test methods raise RuntimeError if client not initialized."""
        a = DiscordAdapter()
        assert a._client is None

        with pytest.raises(RuntimeError):
            await a._rate_limited_request("/route")

        with pytest.raises(RuntimeError):
            await a.send_message(create_mock_message())

        with pytest.raises(RuntimeError):
            await a.send_attachment(create_mock_message(), MagicMock(), MagicMock())

    @pytest.mark.asyncio
    async def test_send_attachment(self, adapter, tmp_path):
        """Test sending file attachment."""
        file_path = tmp_path / "test.txt"
        file_path.write_text("hello")

        msg = create_mock_message(content="Upload")

        attachment = MagicMock()
        attachment.filename = "test.txt"
        attachment.content_type = "text/plain"

        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "attach-1"}
        mock_response.headers = {}

        # Patch the rate_limited_request explicitly
        with patch.object(adapter, "_rate_limited_request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = mock_response

            res = await adapter.send_attachment(msg, attachment, file_path)

            assert res == "attach-1"
            mock_req.assert_called_once()

            # Check route and kwargs
            args, kwargs = mock_req.call_args
            assert args[0] == "/channels/chan_1/messages"
            assert "data" in kwargs
            assert "payload_json" in kwargs["data"]
            assert "files" in kwargs
            # the files dict should have our file info
            assert kwargs["files"]["files[0]"][0] == "test.txt"
            assert kwargs["files"]["files[0]"][1] == b"hello"

    def test_parse_webhook_reaction(self, adapter):
        """Test parsing of MESSAGE_REACTION_ADD."""
        payload = {
            "t": "MESSAGE_REACTION_ADD",
            "d": {
                "user_id": "user1",
                "channel_id": "chan1",
                "message_id": "msg1",
                "emoji": {"name": "👍"},
            },
        }

        res = adapter.parse_webhook(payload, {})
        assert len(res) == 1
        msg = res[0]
        assert msg.content_type == "reaction"
        assert msg.content == "👍"
        assert msg.identity_id == "user1"
        assert msg.platform_message_id == "msg1"
        assert msg.channel_id == ""
        assert msg.metadata_json["platform_channel_id"] == "chan1"

    def test_parse_webhook_missing_data(self, adapter):
        """Test payload structures with varying structures."""
        # direct content
        payload = {
            "content": "hello",
            "author": {"id": "user1"},
            "channel_id": "chan1",
            "id": "msg1",
        }
        res = adapter.parse_webhook(payload, {})
        assert len(res) == 1
        assert res[0].content == "hello"

        # string bytes
        res2 = adapter.parse_webhook(json.dumps(payload).encode(), {})
        assert len(res2) == 1
        assert res2[0].content == "hello"

        # invalid JSON
        with pytest.raises(ValueError, match="Invalid JSON"):
            adapter.parse_webhook(b"{invalid", {})
