"""Tests for the SMS communications adapter."""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import pytest

from gobby.communications.adapters.sms import SMSAdapter
from gobby.communications.models import ChannelConfig, CommsMessage

pytestmark = pytest.mark.unit

_TS = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture
def channel_config() -> ChannelConfig:
    return ChannelConfig(
        id="test_channel",
        channel_type="sms",
        name="Test SMS",
        enabled=True,
        config_json={
            "account_sid": "test-account-sid",
            "auth_token": "$secret:COMMS_SMS_AUTH_TOKEN_TEST",
            "from_number": "+1234567890",
        },
        created_at=_TS,
        updated_at=_TS,
    )


@pytest.fixture
def secret_resolver() -> Any:
    def resolver(key: str) -> str | None:
        if key == "$secret:COMMS_SMS_AUTH_TOKEN_TEST":
            return "test-auth-token"
        return None

    return resolver


@pytest.fixture
def adapter() -> SMSAdapter:
    return SMSAdapter()


def twilio_signature(url: str, params: dict[str, str], secret: str) -> str:
    data = url
    for key in sorted(params.keys()):
        data += f"{key}{params[key]}"

    mac = hmac.new(secret.encode("utf-8"), data.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("utf-8")


@pytest.mark.asyncio
async def test_initialize_success(
    adapter: SMSAdapter, channel_config: ChannelConfig, secret_resolver: Any
) -> None:
    """Test successful initialization."""
    await adapter.initialize(channel_config, secret_resolver)

    assert adapter._account_sid == "test-account-sid"
    assert adapter._auth_token == "test-auth-token"
    assert adapter._from_number == "+1234567890"
    assert adapter._client is not None
    assert (
        str(adapter._client.base_url)
        == "https://api.twilio.com/2010-04-01/Accounts/test-account-sid/"
    )


@pytest.mark.asyncio
async def test_initialize_missing_secrets(
    adapter: SMSAdapter, channel_config: ChannelConfig
) -> None:
    """Test initialization with missing secrets."""

    def empty_resolver(key: str) -> str | None:
        return None

    with pytest.raises(ValueError, match="TWILIO_AUTH_TOKEN secret is required"):
        await adapter.initialize(channel_config, empty_resolver)


@pytest.mark.asyncio
async def test_initialize_missing_config(adapter: SMSAdapter, secret_resolver: Any) -> None:
    """Test initialization with missing config params."""
    config = ChannelConfig(
        id="test_channel",
        channel_type="sms",
        name="Test SMS",
        enabled=True,
        config_json={"auth_token": "$secret:COMMS_SMS_AUTH_TOKEN_TEST"},
        created_at=_TS,
        updated_at=_TS,
    )
    with pytest.raises(ValueError, match="account_sid is required in config_json"):
        await adapter.initialize(config, secret_resolver)


@pytest.mark.asyncio
async def test_send_message_success(
    adapter: SMSAdapter, channel_config: ChannelConfig, secret_resolver: Any
) -> None:
    """Test sending a text message."""
    await adapter.initialize(channel_config, secret_resolver)

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"sid": "SM12345"}
        mock_post.return_value = mock_response

        message = CommsMessage(
            id="1",
            channel_id="gobby-internal-channel",
            direction="outbound",
            content="Hello world",
            created_at=_TS,
            metadata_json={"platform_destination": "+0987654321"},
        )

        msg_id = await adapter.send_message(message)

        assert msg_id == "SM12345"
        mock_post.assert_called_with(
            "Messages.json",
            data={
                "To": "+0987654321",
                "From": "+1234567890",
                "Body": "Hello world",
            },
        )


def test_parse_webhook(adapter: SMSAdapter) -> None:
    """Test parsing a Twilio webhook."""
    payload = {
        "From": "+0987654321",
        "To": "+1234567890",
        "Body": "Hello from Twilio!",
        "MessageSid": "SM12345",
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    msg = messages[0]
    assert msg.channel_id == ""
    assert msg.direction == "inbound"
    assert msg.content == "Hello from Twilio!"
    assert msg.platform_message_id == "SM12345"
    assert msg.identity_id == "+0987654321"
    assert msg.metadata_json["platform_channel_id"] == "+0987654321"
    assert msg.metadata_json["opt_out_action"] is None


def test_parse_webhook_accepts_media_only_mms(adapter: SMSAdapter) -> None:
    """Media-only MMS with blank body still produces an inbound message."""
    payload_str = urlencode(
        {
            "From": "+0987654321",
            "To": "+1234567890",
            "Body": "",
            "MessageSid": "SMmedia",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/media/image.png",
        }
    )

    messages = adapter.parse_webhook(payload_str.encode("utf-8"), {})

    assert len(messages) == 1
    msg = messages[0]
    assert msg.content == ""
    assert msg.platform_message_id == "SMmedia"
    assert msg.identity_id == "+0987654321"
    assert msg.metadata_json["Body"] == ""
    assert msg.metadata_json["NumMedia"] == "1"
    assert msg.metadata_json["MediaUrl0"] == "https://api.twilio.com/media/image.png"


@pytest.mark.parametrize("keyword", ["STOP", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"])
def test_parse_webhook_opt_out(adapter: SMSAdapter, keyword: str) -> None:
    """Test opt-out keyword detection."""
    payload = {"From": "+1111111111", "Body": keyword, "MessageSid": "SM1"}
    messages = adapter.parse_webhook(payload, {})
    assert len(messages) == 1
    assert messages[0].metadata_json["opt_out_action"] == "opt_out"


@pytest.mark.parametrize("keyword", ["START", "UNSTOP", "YES"])
def test_parse_webhook_opt_in(adapter: SMSAdapter, keyword: str) -> None:
    """Test opt-in keyword detection."""
    payload = {"From": "+1111111111", "Body": keyword, "MessageSid": "SM1"}
    messages = adapter.parse_webhook(payload, {})
    assert len(messages) == 1
    assert messages[0].metadata_json["opt_out_action"] == "opt_in"


def test_parse_webhook_opt_out_case_insensitive(adapter: SMSAdapter) -> None:
    """Test opt-out keywords are case insensitive."""
    payload = {"From": "+1111111111", "Body": "stop", "MessageSid": "SM1"}
    messages = adapter.parse_webhook(payload, {})
    assert messages[0].metadata_json["opt_out_action"] == "opt_out"


@pytest.mark.asyncio
async def test_messaging_service_sid(secret_resolver: Any) -> None:
    """Test MessagingServiceSid is used when configured."""
    config = ChannelConfig(
        id="test_channel",
        channel_type="sms",
        name="Test SMS",
        enabled=True,
        config_json={
            "account_sid": "test-account-sid",
            "auth_token": "$secret:COMMS_SMS_AUTH_TOKEN_TEST",
            "messaging_service_sid": "MG12345",
        },
        created_at=_TS,
        updated_at=_TS,
    )

    adapter = SMSAdapter()
    await adapter.initialize(config, secret_resolver)

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"sid": "SM99"}
        mock_post.return_value = mock_response

        message = CommsMessage(
            id="1",
            channel_id="gobby-internal-channel",
            direction="outbound",
            content="Hello",
            created_at=_TS,
            metadata_json={"platform_destination": "+0987654321"},
        )
        await adapter.send_message(message)

        call_data = mock_post.call_args[1]["data"]
        assert call_data["MessagingServiceSid"] == "MG12345"
        assert "From" not in call_data


def test_parse_webhook_no_opt_action(adapter: SMSAdapter) -> None:
    payload = {
        "From": "+0987654321",
        "To": "+1234567890",
        "Body": "Hello",
        "MessageSid": "SM123",
    }
    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    assert messages[0].content == "Hello"
    assert messages[0].metadata_json["opt_out_action"] is None


def test_verify_webhook(adapter: SMSAdapter) -> None:
    """Test verify Twilio webhook signature."""
    secret = "my-secret"
    url = "https://example.com/webhook"

    payload_dict = {
        "From": "+0987654321",
        "Body": "Hello!",
    }
    payload_str = urlencode(payload_dict)

    headers = {
        "x-twilio-signature": twilio_signature(url, payload_dict, secret),
        "x-original-url": url,
    }

    assert adapter.verify_webhook(payload_str.encode("utf-8"), headers, secret) is True

    # Test invalid signature
    headers["x-twilio-signature"] = "invalid"
    assert adapter.verify_webhook(payload_str.encode("utf-8"), headers, secret) is False


def test_verify_webhook_uses_initialized_auth_token(adapter: SMSAdapter) -> None:
    """Twilio webhook verification can use the auth token resolved during initialize."""
    auth_token = "my-secret"
    adapter._auth_token = auth_token
    url = "https://example.com/webhook"
    payload_dict = {
        "From": "+0987654321",
        "Body": "Hello!",
    }
    payload_str = urlencode(payload_dict)
    headers = {
        "x-twilio-signature": twilio_signature(url, payload_dict, auth_token),
        "x-original-url": url,
    }

    assert adapter.verify_webhook(payload_str.encode("utf-8"), headers, "") is True


def test_verify_webhook_keeps_blank_params(adapter: SMSAdapter) -> None:
    """Blank form values are part of Twilio's signature input."""
    secret = "my-secret"
    url = "https://example.com/webhook"
    payload_dict = {
        "From": "+0987654321",
        "Body": "",
        "NumMedia": "0",
    }
    payload_str = urlencode(payload_dict)
    headers = {
        "x-twilio-signature": twilio_signature(url, payload_dict, secret),
        "x-original-url": url,
    }

    assert adapter.verify_webhook(payload_str.encode("utf-8"), headers, secret) is True


def test_verify_webhook_uses_config_url(adapter: SMSAdapter) -> None:
    """verify_webhook uses config webhook_url as primary source."""
    secret = "my-secret"
    config_url = "https://my-gobby.example.com/api/comms/webhooks/sms"
    adapter._webhook_url = config_url

    payload_dict = {"From": "+1234", "Body": "Hi"}
    payload_str = urlencode(payload_dict)

    # No URL headers — config URL should be used
    headers = {"x-twilio-signature": twilio_signature(config_url, payload_dict, secret)}

    assert adapter.verify_webhook(payload_str.encode("utf-8"), headers, secret) is True


def test_verify_webhook_no_url_returns_false(adapter: SMSAdapter) -> None:
    """verify_webhook returns False with warning when no URL available."""
    headers = {"x-twilio-signature": "some-sig"}
    assert adapter.verify_webhook(b"Body=hello", headers, "secret") is False
