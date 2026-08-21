import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest

from gobby.communications.adapters.teams import TeamsAdapter
from gobby.communications.models import ChannelConfig, CommsMessage

pytestmark = pytest.mark.unit


@pytest.fixture
def adapter() -> TeamsAdapter:
    return TeamsAdapter()


@pytest.fixture
def mock_secret_resolver() -> Callable[[str], str | None]:
    def _resolve(secret_ref: str) -> str | None:
        if secret_ref == "$secret:TEAMS_APP_ID":
            return "app_id_123"
        elif secret_ref == "$secret:TEAMS_APP_PASSWORD":
            return "app_pass_456"
        return None

    return _resolve


def _teams_config() -> ChannelConfig:
    return ChannelConfig(
        id="test",
        channel_type="teams",
        name="test",
        enabled=True,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
        updated_at=datetime(2024, 1, 1, tzinfo=UTC),
        config_json={
            "app_id": "$secret:TEAMS_APP_ID",
            "app_password": "$secret:TEAMS_APP_PASSWORD",
        },
    )


@pytest.mark.asyncio
async def test_initialize_and_refresh(
    adapter: TeamsAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    config = _teams_config()

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "token_123", "expires_in": 3600}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        await adapter.initialize(config, mock_secret_resolver)

        assert adapter._app_id == "app_id_123"
        assert adapter._access_token == "token_123"
        assert adapter._client is not None
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"


@pytest.mark.asyncio
async def test_concurrent_refresh(
    adapter: TeamsAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    config = _teams_config()

    # Initial initialize
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.json.return_value = {"access_token": "token_1", "expires_in": 3600}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        await adapter.initialize(config, mock_secret_resolver)

    # Force expiration
    adapter._token_expires_at = 0

    call_count = 0

    async def counting_post(*args: str, **kwargs: object) -> MagicMock:
        nonlocal call_count
        # First call is the token refresh, subsequent are the send
        call_count += 1
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {}
        if args and "oauth2" in args[0]:
            mock_resp.json.return_value = {"access_token": "token_2", "expires_in": 3600}
        else:
            mock_resp.json.return_value = {"id": "msg_1"}
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    msg = CommsMessage(
        id="test_id",
        channel_id="gobby-internal-channel",
        direction="outbound",
        content="Hello",
        metadata_json={
            "service_url": "https://smba.trafficmanager.net/teams/",
            "platform_destination": "conv_123",
        },
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )

    with patch.object(adapter._client, "post", side_effect=counting_post):
        # Concurrent send_messages — lock ensures only one refresh
        await asyncio.gather(
            adapter.send_message(msg), adapter.send_message(msg), adapter.send_message(msg)
        )

    # 1 token refresh + 3 send_message posts = 4 total calls
    assert call_count == 4
    assert adapter._access_token == "token_2"


@pytest.mark.asyncio
async def test_send_message(
    adapter: TeamsAdapter, mock_secret_resolver: Callable[[str], str | None]
) -> None:
    config = _teams_config()

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_response_auth = MagicMock()
        mock_response_auth.json.return_value = {"access_token": "token_123", "expires_in": 3600}
        mock_response_auth.raise_for_status.return_value = None
        mock_post.return_value = mock_response_auth

        await adapter.initialize(config, mock_secret_resolver)

    msg = CommsMessage(
        id="test_id",
        channel_id="gobby-internal-channel",
        direction="outbound",
        content="Hello teams",
        metadata_json={
            "service_url": "https://smba.trafficmanager.net/teams/",
            "platform_destination": "conv_123",
        },
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )

    with patch.object(adapter._client, "post", new_callable=AsyncMock) as mock_post:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.json.return_value = {"id": "msg_456"}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        result = await adapter.send_message(msg)

        assert result == "msg_456"
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "conv_123/activities" in args[0]
        assert kwargs["json"]["text"] == "Hello teams"
        assert kwargs["headers"]["Authorization"] == "Bearer token_123"


def test_parse_webhook(adapter: TeamsAdapter) -> None:
    payload = {
        "type": "message",
        "id": "msg_123",
        "from": {"id": "user_123"},
        "conversation": {"id": "conv_123"},
        "text": "Hello bot",
        "serviceUrl": "https://smba.trafficmanager.net/teams/",
    }

    messages = adapter.parse_webhook(payload, {})

    assert len(messages) == 1
    assert messages[0].channel_id == ""
    assert messages[0].metadata_json["platform_channel_id"] == "conv_123"
    assert messages[0].content == "Hello bot"
    assert messages[0].identity_id == "user_123"
    assert messages[0].metadata_json["service_url"] == "https://smba.trafficmanager.net/teams/"


def test_verify_webhook(adapter: TeamsAdapter) -> None:
    adapter._app_id = "app_id_123"

    mock_jwk_client = MagicMock()
    mock_signing_key = MagicMock()
    mock_signing_key.key = "test-key"
    mock_jwk_client.get_signing_key_from_jwt.return_value = mock_signing_key

    adapter._jwk_client = mock_jwk_client

    with patch("jwt.decode") as mock_decode:
        # Valid case
        service_url = "https://smba.trafficmanager.net/teams/"
        mock_decode.return_value = {
            "aud": "app_id_123",
            "iss": "https://api.botframework.com",
            "serviceUrl": service_url,
        }
        headers = {"Authorization": "Bearer some.jwt.token"}
        result = adapter.verify_webhook(
            json.dumps({"serviceUrl": service_url}).encode(),
            headers,
            "secret",
        )
        assert result

        # jwt.decode is called with proper signature verification args
        mock_decode.assert_called_with(
            "some.jwt.token",
            "test-key",
            algorithms=["RS256"],
            audience="app_id_123",
            issuer="https://api.botframework.com",
        )

        # Missing auth header
        result = adapter.verify_webhook(b"", {}, "secret")
        assert not result

    # JWT verification failure returns False
    mock_jwk_client.get_signing_key_from_jwt.side_effect = jwt.PyJWKClientError("bad token")
    headers = {"Authorization": "Bearer bad.jwt.token"}
    result = adapter.verify_webhook(b"", headers, "secret")
    assert not result


def test_verify_webhook_real_rsa_jwt_rejects_bad_audience_or_issuer(
    adapter: TeamsAdapter,
) -> None:
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signing_key = MagicMock()
    signing_key.key = private_key.public_key()
    jwk_client = MagicMock()
    jwk_client.get_signing_key_from_jwt.return_value = signing_key
    adapter._app_id = "app_id_123"
    adapter._jwk_client = jwk_client

    service_url = "https://smba.trafficmanager.net/teams/"
    claims = {
        "aud": "app_id_123",
        "iss": "https://api.botframework.com",
        "serviceUrl": service_url,
    }
    body = json.dumps({"serviceUrl": service_url}).encode()
    valid_token = jwt.encode(claims, private_key, algorithm="RS256")
    assert adapter.verify_webhook(body, {"Authorization": f"Bearer {valid_token}"}, "secret")

    wrong_aud = jwt.encode({**claims, "aud": "other-app"}, private_key, algorithm="RS256")
    assert not adapter.verify_webhook(body, {"Authorization": f"Bearer {wrong_aud}"}, "secret")

    wrong_iss = jwt.encode(
        {**claims, "iss": "https://evil.example"}, private_key, algorithm="RS256"
    )
    assert not adapter.verify_webhook(body, {"Authorization": f"Bearer {wrong_iss}"}, "secret")


def test_verify_webhook_configures_jwks_timeout(adapter: TeamsAdapter) -> None:
    adapter._app_id = "app_id_123"

    with patch("gobby.communications.adapters.teams.PyJWKClient") as mock_jwk_client:
        mock_instance = mock_jwk_client.return_value
        mock_instance.get_signing_key_from_jwt.side_effect = jwt.PyJWKClientError("bad token")

        result = adapter.verify_webhook(b"{}", {"Authorization": "Bearer bad.jwt.token"}, "secret")

    assert not result
    mock_jwk_client.assert_called_once_with(
        "https://login.botframework.com/v1/.well-known/keys",
        timeout=5.0,
    )
