from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from gobby.cli.communications import comms

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_daemon_client():
    with patch("gobby.cli.communications.get_daemon_client") as mock:
        client = MagicMock()
        mock.return_value = client
        yield client


def test_comms_status_success(mock_daemon_client):
    runner = CliRunner()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {
            "name": "test-channel",
            "channel_type": "telegram",
            "enabled": True,
            "active": True,
            "init_error": None,
        }
    ]
    mock_daemon_client.call_http_api.return_value = mock_response

    result = runner.invoke(comms, ["status"])

    assert result.exit_code == 0
    assert "test-channel" in result.output
    assert "telegram" in result.output
    assert "active" in result.output
    mock_daemon_client.call_http_api.assert_called_once_with("/api/comms/channels", method="GET")


def test_comms_send_success(mock_daemon_client):
    runner = CliRunner()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_daemon_client.call_http_api.return_value = mock_response

    result = runner.invoke(comms, ["send", "test-channel", "hello world"])

    assert result.exit_code == 0
    assert "Message sent to test-channel" in result.output
    mock_daemon_client.call_http_api.assert_called_once_with(
        "/api/comms/send",
        method="POST",
        json_data={"channel_name": "test-channel", "content": "hello world"},
    )


def test_comms_channels_list(mock_daemon_client):
    runner = CliRunner()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = [
        {"id": "cc_123", "name": "test-chan", "channel_type": "slack", "enabled": True}
    ]
    mock_daemon_client.call_http_api.return_value = mock_response

    result = runner.invoke(comms, ["channels", "list"])

    assert result.exit_code == 0
    assert "test-chan" in result.output
    assert "slack" in result.output
    assert "cc_123" in result.output


def test_comms_channels_add_telegram(mock_daemon_client):
    runner = CliRunner()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 201
    mock_daemon_client.call_http_api.return_value = mock_response

    result = runner.invoke(
        comms, ["channels", "add", "telegram", "my-tg"], input="mytoken\nmychatid\n"
    )

    assert result.exit_code == 0
    assert "Channel 'my-tg' added successfully" in result.output

    mock_daemon_client.call_http_api.assert_called_once_with(
        "/api/comms/channels",
        method="POST",
        json_data={
            "name": "my-tg",
            "channel_type": "telegram",
            "config": {"default_destination": "mychatid"},
            "secrets": {"bot_token": "mytoken"},
        },
    )


def test_comms_channels_add_slack(mock_daemon_client):
    runner = CliRunner()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 201
    mock_daemon_client.call_http_api.return_value = mock_response

    result = runner.invoke(
        comms,
        ["channels", "add", "slack", "my-slack"],
        input="xoxb-token\nsigning-sec\nC12345\n",
    )

    assert result.exit_code == 0
    mock_daemon_client.call_http_api.assert_called_once_with(
        "/api/comms/channels",
        method="POST",
        json_data={
            "name": "my-slack",
            "channel_type": "slack",
            "config": {"default_destination": "C12345"},
            "secrets": {"bot_token": "xoxb-token", "signing_secret": "signing-sec"},
        },
    )


def test_comms_channels_add_discord(mock_daemon_client: MagicMock) -> None:
    runner = CliRunner()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 201
    mock_daemon_client.call_http_api.return_value = mock_response

    result = runner.invoke(
        comms,
        ["channels", "add", "discord", "my-discord"],
        input="discord-token\n123456789\n",
    )

    assert result.exit_code == 0
    mock_daemon_client.call_http_api.assert_called_once_with(
        "/api/comms/channels",
        method="POST",
        json_data={
            "name": "my-discord",
            "channel_type": "discord",
            "config": {"default_destination": "123456789"},
            "secrets": {"bot_token": "discord-token"},
        },
    )


def test_comms_channels_add_teams(mock_daemon_client):
    runner = CliRunner()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 201
    mock_daemon_client.call_http_api.return_value = mock_response

    result = runner.invoke(
        comms,
        ["channels", "add", "teams", "my-teams"],
        input="app-id-123\napp-pass-456\n",
    )

    assert result.exit_code == 0
    mock_daemon_client.call_http_api.assert_called_once_with(
        "/api/comms/channels",
        method="POST",
        json_data={
            "name": "my-teams",
            "channel_type": "teams",
            "config": {},
            "secrets": {"app_id": "app-id-123", "app_password": "app-pass-456"},
        },
    )


def test_comms_channels_add_email(mock_daemon_client):
    runner = CliRunner()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 201
    mock_daemon_client.call_http_api.return_value = mock_response

    result = runner.invoke(
        comms,
        ["channels", "add", "email", "my-email"],
        input="secret-pw\nsmtp.example.com\n587\nimap.example.com\n993\nme@example.com\n",
    )

    assert result.exit_code == 0
    mock_daemon_client.call_http_api.assert_called_once_with(
        "/api/comms/channels",
        method="POST",
        json_data={
            "name": "my-email",
            "channel_type": "email",
            "config": {
                "smtp_host": "smtp.example.com",
                "smtp_port": 587,
                "imap_host": "imap.example.com",
                "imap_port": 993,
                "from_address": "me@example.com",
            },
            "secrets": {"password": "secret-pw"},
        },
    )


def test_comms_channels_add_sms(mock_daemon_client):
    runner = CliRunner()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 201
    mock_daemon_client.call_http_api.return_value = mock_response

    result = runner.invoke(
        comms,
        ["channels", "add", "sms", "my-sms"],
        input="auth-token-123\nAC123456\n+15551234567\n\n",
    )

    assert result.exit_code == 0
    mock_daemon_client.call_http_api.assert_called_once_with(
        "/api/comms/channels",
        method="POST",
        json_data={
            "name": "my-sms",
            "channel_type": "sms",
            "config": {"account_sid": "AC123456", "from_number": "+15551234567"},
            "secrets": {"auth_token": "auth-token-123"},
        },
    )


def test_comms_channels_add_gobby_chat(mock_daemon_client):
    runner = CliRunner()

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 201
    mock_daemon_client.call_http_api.return_value = mock_response

    result = runner.invoke(comms, ["channels", "add", "gobby_chat", "my-gc"])

    assert result.exit_code == 0
    assert "No additional configuration" in result.output
    mock_daemon_client.call_http_api.assert_called_once_with(
        "/api/comms/channels",
        method="POST",
        json_data={
            "name": "my-gc",
            "channel_type": "gobby_chat",
            "config": {},
            "secrets": None,
        },
    )


def test_comms_channels_add_custom_rejects_non_object_config(mock_daemon_client):
    runner = CliRunner()

    result = runner.invoke(comms, ["channels", "add", "custom", "bad-custom"], input="[]\n")

    assert result.exit_code == 1
    assert "Configuration must be a JSON object." in result.output
    mock_daemon_client.call_http_api.assert_not_called()


def test_comms_channels_remove(mock_daemon_client: MagicMock) -> None:
    runner = CliRunner()

    # First response for listing
    list_response = MagicMock(spec=httpx.Response)
    list_response.status_code = 200
    list_response.json.return_value = [{"id": "cc_123", "name": "my-tg"}]

    # Second response for delete
    delete_response = MagicMock(spec=httpx.Response)
    delete_response.status_code = 204

    mock_daemon_client.call_http_api.side_effect = [list_response, delete_response]

    result = runner.invoke(comms, ["channels", "remove", "my-tg"], input="y\n")

    assert result.exit_code == 0
    assert "removed successfully" in result.output
    assert mock_daemon_client.call_http_api.call_count == 2
    mock_daemon_client.call_http_api.assert_any_call("/api/comms/channels", method="GET")
    mock_daemon_client.call_http_api.assert_any_call("/api/comms/channels/cc_123", method="DELETE")


def test_comms_subscriptions_create_uses_cwd_project(
    mock_daemon_client: MagicMock,
) -> None:
    response = MagicMock(status_code=200)
    response.json.return_value = {"id": "sub-1", "scope": {"kind": "project"}}
    mock_daemon_client.call_http_api.return_value = response

    with patch(
        "gobby.cli.communications.resolve_project_ref",
        return_value="cwd-project-id",
    ) as resolve_project:
        result = CliRunner().invoke(
            comms,
            [
                "subscriptions",
                "create",
                "Agent pauses",
                "--channel",
                "telegram",
                "--event",
                "session.agent.paused",
            ],
        )

    assert result.exit_code == 0
    resolve_project.assert_called_once_with(None)
    mock_daemon_client.call_http_api.assert_called_once_with(
        "/api/comms/subscriptions",
        method="POST",
        json_data={
            "name": "Agent pauses",
            "channel": "telegram",
            "event_pattern": "session.agent.paused",
            "project_id": "cwd-project-id",
            "global_scope": False,
            "session_id": None,
            "priority": 0,
            "enabled": True,
        },
    )


def test_comms_subscriptions_create_requires_project_or_explicit_global(
    mock_daemon_client: MagicMock,
) -> None:
    with patch("gobby.cli.communications.resolve_project_ref", return_value=None):
        missing = CliRunner().invoke(
            comms,
            [
                "subscriptions",
                "create",
                "Agent pauses",
                "--channel",
                "telegram",
                "--event",
                "session.agent.paused",
            ],
        )

    assert missing.exit_code == 1
    assert "No project context found; pass --project or --global." in missing.output
    mock_daemon_client.call_http_api.assert_not_called()

    response = MagicMock(status_code=200)
    response.json.return_value = {"id": "sub-global", "scope": {"kind": "global"}}
    mock_daemon_client.call_http_api.return_value = response
    explicit_global = CliRunner().invoke(
        comms,
        [
            "subscriptions",
            "create",
            "Global agent pauses",
            "--channel",
            "telegram",
            "--event",
            "session.agent.paused",
            "--global",
        ],
    )

    assert explicit_global.exit_code == 0
    payload = mock_daemon_client.call_http_api.call_args.kwargs["json_data"]
    assert payload["project_id"] is None
    assert payload["global_scope"] is True


def test_comms_subscriptions_list_get_update_and_delete(
    mock_daemon_client: MagicMock,
) -> None:
    runner = CliRunner()
    response = MagicMock(status_code=200)
    mock_daemon_client.call_http_api.return_value = response

    response.json.return_value = [{"id": "sub-1"}]
    listed = runner.invoke(
        comms,
        ["subscriptions", "list", "--channel", "telegram", "--disabled"],
    )
    assert listed.exit_code == 0
    mock_daemon_client.call_http_api.assert_called_with(
        "/api/comms/subscriptions?channel=telegram&enabled=False",
        method="GET",
    )

    response.json.return_value = {"id": "sub-1"}
    fetched = runner.invoke(comms, ["subscriptions", "get", "sub-1"])
    assert fetched.exit_code == 0
    mock_daemon_client.call_http_api.assert_called_with(
        "/api/comms/subscriptions/sub-1",
        method="GET",
    )

    updated = runner.invoke(
        comms,
        ["subscriptions", "update", "sub-1", "--priority", "10", "--disabled"],
    )
    assert updated.exit_code == 0
    mock_daemon_client.call_http_api.assert_called_with(
        "/api/comms/subscriptions/sub-1",
        method="PATCH",
        json_data={"priority": 10, "enabled": False},
    )

    response.json.return_value = {"status": "ok", "deleted": "sub-1"}
    deleted = runner.invoke(comms, ["subscriptions", "delete", "sub-1"])
    assert deleted.exit_code == 0
    mock_daemon_client.call_http_api.assert_called_with(
        "/api/comms/subscriptions/sub-1",
        method="DELETE",
    )
