from dataclasses import asdict
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from gobby.communications.adapters.slack import SlackAdapter
from gobby.communications.models import ChannelConfig, CommsMessage
from gobby.config.app import DaemonConfig
from gobby.servers.auth_service import AuthService
from tests.servers.conftest import create_http_server


@pytest.fixture
def comms_manager():
    manager = MagicMock()
    manager.handle_inbound = AsyncMock(return_value=[])
    manager.add_channel = AsyncMock()
    manager.update_channel = AsyncMock()
    manager.remove_channel = AsyncMock()
    manager._store = MagicMock()

    def channel_to_dict(channel):
        payload = asdict(channel)
        payload.pop("webhook_secret", None)
        payload["active"] = False
        payload["init_error"] = None
        return payload

    manager.channel_to_dict.side_effect = channel_to_dict

    # Mock some store methods
    manager.list_channels.return_value = []
    manager.get_channel_status.return_value = {"status": "active"}

    return manager


@pytest.fixture
def server(comms_manager, temp_db):
    """HTTPServer with mocked comms manager."""
    config = DaemonConfig()
    config.communications.enabled = True

    srv = create_http_server(config=config, database=temp_db)
    srv.services.communications_manager = comms_manager
    return srv


@pytest.fixture
def client(server):
    return TestClient(server.app)


@pytest.fixture
def ui_auth_client(server: Any, tmp_path) -> TestClient:
    server.auth_service = AuthService(
        lambda: server.services.database,
        "required",
        token_file=tmp_path / "missing-local-token",
    )
    return TestClient(server.app)


def test_receive_webhook_ok(client, comms_manager):
    comms_manager.handle_inbound.return_value = [
        CommsMessage(
            id="msg1",
            channel_id="ch1",
            direction="inbound",
            content="hello",
            created_at="2023-01-01T00:00:00Z",
        )
    ]
    response = client.post("/api/comms/webhooks/slack", json={"text": "hello"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "messages": 1}
    comms_manager.handle_inbound.assert_called_once()


def test_receive_webhook_bypasses_ui_auth(ui_auth_client, comms_manager):
    protected_response = ui_auth_client.get("/api/comms/channels")
    assert protected_response.status_code == 401

    response = ui_auth_client.post("/api/comms/webhooks/slack", json={"text": "hello"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "messages": 0}
    comms_manager.handle_inbound.assert_called_once()


def test_receive_webhook_rejects_unverified_channel(client, comms_manager):
    comms_manager.handle_inbound.side_effect = ValueError("invalid webhook signature")

    response = client.post("/api/comms/webhooks/slack", json={"text": "unverified"})

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid webhook signature"


def test_receive_webhook_url_verification(client, comms_manager):
    adapter = SlackAdapter()

    async def parse_slack_webhook(
        _channel_name: str,
        payload: dict[str, Any] | bytes,
        headers: dict[str, str],
        raw_body: bytes | None = None,
    ) -> list[CommsMessage]:
        _ = raw_body
        return adapter.parse_webhook(payload, headers)

    comms_manager.handle_inbound.side_effect = parse_slack_webhook

    response = client.post(
        "/api/comms/webhooks/slack",
        json={"type": "url_verification", "challenge": "challenge_token"},
    )
    assert response.status_code == 200
    assert response.text == "challenge_token"


def test_verify_webhook_get(client):
    response = client.get("/api/comms/webhooks/teams?validationToken=token123")
    assert response.status_code == 200
    assert response.text == "token123"

    response = client.get("/api/comms/webhooks/slack?challenge=chal123")
    assert response.status_code == 200
    assert response.text == "chal123"


def test_send_message(client, comms_manager):
    message = CommsMessage(
        id="msg1",
        channel_id="ch1",
        direction="outbound",
        content="hello",
        created_at="2023-01-01T00:00:00Z",
    )
    comms_manager.send_message = AsyncMock(return_value=message)

    response = client.post("/api/comms/send", json={"channel_name": "alerts", "content": "hello"})

    assert response.status_code == 200
    assert response.json()["id"] == "msg1"
    comms_manager.send_message.assert_awaited_once_with("alerts", "hello")


def test_send_message_unknown_channel(client, comms_manager):
    comms_manager.send_message = AsyncMock(
        side_effect=ValueError("Channel 'missing' not found or not active")
    )

    response = client.post("/api/comms/send", json={"channel_name": "missing", "content": "hello"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Channel 'missing' not found or not active"}


def test_list_channels(client, comms_manager):
    ch = ChannelConfig(
        id="ch1",
        channel_type="slack",
        name="myslack",
        enabled=True,
        config_json={},
        created_at="2023-01-01T00:00:00Z",
        updated_at="2023-01-01T00:00:00Z",
        webhook_secret="$secret:COMMS_SLACK_WEBHOOK_SECRET_MYSLACK",
    )
    comms_manager.list_channels.return_value = [ch]

    response = client.get("/api/comms/channels")
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert response.json()[0]["id"] == "ch1"
    assert "webhook_secret" not in response.json()[0]


def test_create_channel(client, comms_manager):
    ch = ChannelConfig(
        id="ch1",
        channel_type="slack",
        name="myslack",
        enabled=True,
        config_json={"foo": "bar"},
        created_at="2023-01-01T00:00:00Z",
        updated_at="2023-01-01T00:00:00Z",
        webhook_secret="$secret:COMMS_SLACK_WEBHOOK_SECRET_MYSLACK",
    )
    comms_manager.add_channel.return_value = ch

    response = client.post(
        "/api/comms/channels",
        json={"channel_type": "slack", "name": "myslack", "config": {"foo": "bar"}},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "ch1"
    assert "webhook_secret" not in response.json()
    comms_manager.add_channel.assert_called_once()


def test_create_channel_reports_init_error(client, comms_manager):
    ch = ChannelConfig(
        id="ch1",
        channel_type="slack",
        name="myslack",
        enabled=True,
        config_json={"foo": "bar"},
        created_at="2023-01-01T00:00:00Z",
        updated_at="2023-01-01T00:00:00Z",
    )
    comms_manager.add_channel.return_value = ch
    comms_manager.channel_to_dict.return_value = {
        **asdict(ch),
        "active": False,
        "init_error": "bad token",
    }
    comms_manager.channel_to_dict.side_effect = None

    response = client.post(
        "/api/comms/channels",
        json={"channel_type": "slack", "name": "myslack", "config": {"foo": "bar"}},
    )

    assert response.status_code == 200
    assert response.json()["active"] is False
    assert response.json()["init_error"] == "bad token"


def test_update_channel(client, comms_manager):
    ch = ChannelConfig(
        id="ch1",
        channel_type="slack",
        name="myslack",
        enabled=True,
        config_json={},
        created_at="2023-01-01T00:00:00Z",
        updated_at="2023-01-01T00:00:00Z",
        webhook_secret="$secret:COMMS_SLACK_WEBHOOK_SECRET_MYSLACK",
    )
    comms_manager.get_channel.return_value = ch
    comms_manager.update_channel.return_value = ch

    response = client.put(
        "/api/comms/channels/ch1", json={"config": {"foo": "baz"}, "enabled": False}
    )

    assert response.status_code == 200
    assert response.json()["id"] == "ch1"
    assert "webhook_secret" not in response.json()
    comms_manager.update_channel.assert_called_once_with(ch, secrets=None)
    assert ch.config_json == {"foo": "baz"}
    assert ch.enabled is False


def test_update_channel_preserves_secret_refs_when_editing_non_secret_config(client, comms_manager):
    ch = ChannelConfig(
        id="ch1",
        channel_type="slack",
        name="myslack",
        enabled=True,
        config_json={
            "channel_id": "C123",
            "bot_token": "$secret:COMMS_SLACK_BOT_TOKEN_MYSLACK",
        },
        created_at="2023-01-01T00:00:00Z",
        updated_at="2023-01-01T00:00:00Z",
    )
    comms_manager.get_channel.return_value = ch
    comms_manager.update_channel.return_value = ch

    response = client.put(
        "/api/comms/channels/ch1",
        json={"config": {"channel_id": "C999"}},
    )

    assert response.status_code == 200
    assert ch.config_json == {
        "channel_id": "C999",
        "bot_token": "$secret:COMMS_SLACK_BOT_TOKEN_MYSLACK",
    }
    comms_manager.update_channel.assert_called_once_with(ch, secrets=None)


def test_update_channel_forwards_secret_changes(client, comms_manager):
    ch = ChannelConfig(
        id="ch1",
        channel_type="slack",
        name="myslack",
        enabled=True,
        config_json={},
        created_at="2023-01-01T00:00:00Z",
        updated_at="2023-01-01T00:00:00Z",
    )
    comms_manager.get_channel.return_value = ch
    comms_manager.update_channel.return_value = ch

    response = client.put("/api/comms/channels/ch1", json={"secrets": {"bot_token": "new-token"}})

    assert response.status_code == 200
    comms_manager.update_channel.assert_called_once_with(ch, secrets={"bot_token": "new-token"})


def test_update_channel_partial_config_only(client, comms_manager):
    """Test that partial updates (only config, not enabled) don't overwrite with None."""
    ch = ChannelConfig(
        id="ch1",
        channel_type="slack",
        name="myslack",
        enabled=True,
        config_json={"old": "value"},
        created_at="2023-01-01T00:00:00Z",
        updated_at="2023-01-01T00:00:00Z",
    )
    comms_manager.get_channel.return_value = ch
    comms_manager.update_channel.return_value = ch

    response = client.put("/api/comms/channels/ch1", json={"config": {"new": "config"}})

    assert response.status_code == 200
    assert ch.config_json == {"new": "config"}
    assert ch.enabled is True  # Not overwritten to None


def test_update_channel_partial_enabled_only(client, comms_manager):
    """Test that partial updates (only enabled, not config) don't overwrite with None."""
    ch = ChannelConfig(
        id="ch1",
        channel_type="slack",
        name="myslack",
        enabled=True,
        config_json={"keep": "this"},
        created_at="2023-01-01T00:00:00Z",
        updated_at="2023-01-01T00:00:00Z",
    )
    comms_manager.get_channel.return_value = ch
    comms_manager.update_channel.return_value = ch

    response = client.put("/api/comms/channels/ch1", json={"enabled": False})

    assert response.status_code == 200
    assert ch.enabled is False
    assert ch.config_json == {"keep": "this"}  # Not overwritten to None


def test_update_channel_not_found(client, comms_manager):
    comms_manager.get_channel.return_value = None

    response = client.put("/api/comms/channels/ch1", json={"enabled": False})
    assert response.status_code == 404


def test_remove_channel(client, comms_manager):
    ch = ChannelConfig(
        id="ch1",
        channel_type="slack",
        name="myslack",
        enabled=True,
        config_json={},
        created_at="2023-01-01T00:00:00Z",
        updated_at="2023-01-01T00:00:00Z",
    )
    comms_manager.get_channel.return_value = ch

    response = client.delete("/api/comms/channels/ch1")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    comms_manager.remove_channel.assert_called_once_with("myslack")


def test_get_channel_status(client, comms_manager):
    ch = ChannelConfig(
        id="ch1",
        channel_type="slack",
        name="myslack",
        enabled=True,
        config_json={},
        created_at="2023-01-01T00:00:00Z",
        updated_at="2023-01-01T00:00:00Z",
    )
    comms_manager.get_channel.return_value = ch
    comms_manager.get_channel_status.return_value = {"name": "myslack", "status": "active"}

    response = client.get("/api/comms/channels/ch1/status")
    assert response.status_code == 200
    assert response.json()["status"] == "active"


def test_list_messages(client, comms_manager):
    msg = CommsMessage(
        id="msg1",
        channel_id="ch1",
        direction="outbound",
        content="test",
        created_at="2023-01-01T00:00:00Z",
    )
    comms_manager.list_messages.return_value = [msg]

    response = client.get("/api/comms/messages?channel_id=ch1")
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["id"] == "msg1"
    comms_manager.list_messages.assert_called_once_with(
        channel_id="ch1", session_id=None, direction=None, limit=50, offset=0
    )
