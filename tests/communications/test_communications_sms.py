import base64
import hashlib
import hmac
from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlencode

import httpx
import pytest

from gobby.communications.adapters.sms import SMSAdapter
from gobby.communications.models import ChannelConfig, CommsMessage

pytestmark = pytest.mark.unit


@pytest.fixture
def adapter() -> SMSAdapter:
    return SMSAdapter()


@pytest.fixture
def mock_secret_resolver() -> Callable[[str], str | None]:
    def _resolve(secret_ref: str) -> str | None:
        if secret_ref == "$secret:TWILIO_AUTH_TOKEN":
            return "token_123"
        return None

    return _resolve


@pytest.mark.asyncio
async def test_initialize(
    adapter: SMSAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    config = ChannelConfig(
        id="test",
        channel_type="sms",
        name="test",
        enabled=True,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        updated_at=datetime(2024, 1, 1, tzinfo=UTC),
        config_json={
            "account_sid": "AC123",
            "auth_token": "$secret:TWILIO_AUTH_TOKEN",
            "from_number": "+1234567890",
        },
    )

    await adapter.initialize(config, mock_secret_resolver)

    assert adapter._auth_token == "token_123"
    assert adapter._account_sid == "AC123"
    assert adapter._from_number == "+1234567890"
    assert adapter._client is not None
    assert isinstance(adapter._client.auth, httpx.BasicAuth)


@pytest.mark.asyncio
async def test_send_message(
    adapter: SMSAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    config = ChannelConfig(
        id="test",
        channel_type="sms",
        name="test",
        enabled=True,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        updated_at=datetime(2024, 1, 1, tzinfo=UTC),
        config_json={
            "account_sid": "AC123",
            "auth_token": "$secret:TWILIO_AUTH_TOKEN",
            "from_number": "+1234567890",
            "to_number": "+0987654321",
        },
    )
    await adapter.initialize(config, mock_secret_resolver)

    msg = CommsMessage(
        id="test_id",
        channel_id="gobby-internal-channel",
        direction="outbound",
        content="Hello SMS",
        metadata_json={"platform_destination": "+0987654321"},
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )

    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json.return_value = {"sid": "SM123"}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        result = await adapter.send_message(msg)

        assert result == "SM123"
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "Messages.json"
        assert kwargs["data"]["To"] == "+0987654321"
        assert kwargs["data"]["From"] == "+1234567890"
        assert kwargs["data"]["Body"] == "Hello SMS"


def test_parse_webhook(adapter: SMSAdapter) -> None:
    payload = {
        "From": "+0987654321",
        "To": "+1234567890",
        "Body": "Hello Twilio",
        "MessageSid": "SM123",
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    assert messages[0].channel_id == ""
    assert messages[0].metadata_json["platform_channel_id"] == "+0987654321"
    assert messages[0].content == "Hello Twilio"
    assert messages[0].platform_message_id == "SM123"
    assert messages[0].identity_id == "+0987654321"


def test_parse_webhook_urlencoded(adapter: SMSAdapter) -> None:
    payload = b"From=%2B0987654321&To=%2B1234567890&Body=Hello+Twilio&MessageSid=SM123"

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    assert messages[0].channel_id == ""
    assert messages[0].metadata_json["platform_channel_id"] == "+0987654321"
    assert messages[0].content == "Hello Twilio"
    assert messages[0].platform_message_id == "SM123"


def test_verify_webhook(adapter: SMSAdapter) -> None:
    url = "https://example.com/webhook"
    secret = "secret123"

    params = {"From": "+123", "To": "+456"}
    payload = urlencode(params).encode("utf-8")

    data = url + "From+123To+456"
    mac = hmac.new(secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha1)
    computed_signature = base64.b64encode(mac.digest()).decode("utf-8")

    headers = {"X-Twilio-Signature": computed_signature, "X-Original-Url": url}

    result = adapter.verify_webhook(payload, headers, secret)
    assert result

    # Invalid signature
    headers["X-Twilio-Signature"] = "invalid"
    result = adapter.verify_webhook(payload, headers, secret)
    assert not result

    # Missing URL
    headers = {"X-Twilio-Signature": computed_signature}
    result = adapter.verify_webhook(payload, headers, secret)
    assert not result
