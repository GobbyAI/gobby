"""Tests for communications CLI commands."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.communications import comms

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    return client


def _mock_response(
    status_code: int = 200, json_data: object | None = None, text: str = ""
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else {}
    resp.text = text
    return resp


# --- comms status ---


def test_status_shows_channels(runner: CliRunner, mock_client: MagicMock) -> None:
    """status command displays channel status table."""
    mock_client.call_http_api.return_value = _mock_response(
        json_data=[
            {
                "name": "my-slack",
                "channel_type": "slack",
                "enabled": True,
                "status": "connected",
                "stats": {"inbound": 42, "outbound": 10},
            }
        ]
    )

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(comms, ["status"])

    assert result.exit_code == 0
    assert "my-slack" in result.output
    assert "slack" in result.output


def test_status_no_channels(runner: CliRunner, mock_client: MagicMock) -> None:
    """status command shows message when no channels configured."""
    mock_client.call_http_api.return_value = _mock_response(json_data=[])

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(comms, ["status"])

    assert result.exit_code == 0
    assert "No communications channels configured" in result.output


def test_status_api_failure(runner: CliRunner, mock_client: MagicMock) -> None:
    """status command handles API errors."""
    mock_client.call_http_api.return_value = _mock_response(
        status_code=500, text="Internal Server Error"
    )

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(comms, ["status"])

    assert result.exit_code == 1
    assert "Error" in result.output


def test_status_connection_failure(runner: CliRunner, mock_client: MagicMock) -> None:
    """status command handles daemon connection failure."""
    import httpx

    mock_client.call_http_api.side_effect = httpx.RequestError("Connection refused")

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(comms, ["status"])

    assert result.exit_code == 1
    assert "Daemon connection failed" in result.output


# --- comms send ---


def test_send_success(runner: CliRunner, mock_client: MagicMock) -> None:
    """send command sends message and shows success."""
    mock_client.call_http_api.return_value = _mock_response(status_code=200)

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(comms, ["send", "my-slack", "Hello world"])

    assert result.exit_code == 0
    assert "Message sent to my-slack" in result.output
    mock_client.call_http_api.assert_called_once_with(
        "/api/comms/send",
        method="POST",
        json_data={"channel_name": "my-slack", "content": "Hello world"},
    )


def test_send_failure(runner: CliRunner, mock_client: MagicMock) -> None:
    """send command handles send failure."""
    mock_client.call_http_api.return_value = _mock_response(
        status_code=400, text="Channel not found"
    )

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(comms, ["send", "nonexistent", "Hello"])

    assert result.exit_code == 1
    assert "Error" in result.output


def test_send_connection_failure(runner: CliRunner, mock_client: MagicMock) -> None:
    """send command handles daemon connection failure."""
    import httpx

    mock_client.call_http_api.side_effect = httpx.RequestError("Connection refused")

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(comms, ["send", "my-slack", "Hello"])

    assert result.exit_code == 1
    assert "Daemon connection failed" in result.output


# --- comms channels list ---


def test_channels_list_shows_table(runner: CliRunner, mock_client: MagicMock) -> None:
    """channels list command displays channel table."""
    mock_client.call_http_api.return_value = _mock_response(
        json_data=[
            {
                "id": "chan-1",
                "name": "my-slack",
                "channel_type": "slack",
                "enabled": True,
            },
            {
                "id": "chan-2",
                "name": "my-telegram",
                "channel_type": "telegram",
                "enabled": False,
            },
        ]
    )

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(comms, ["channels", "list"])

    assert result.exit_code == 0
    assert "my-slack" in result.output
    assert "my-telegram" in result.output
    mock_client.call_http_api.assert_called_once_with("/api/comms/channels", method="GET")


def test_channels_list_empty(runner: CliRunner, mock_client: MagicMock) -> None:
    """channels list shows message when empty."""
    mock_client.call_http_api.return_value = _mock_response(json_data=[])

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(comms, ["channels", "list"])

    assert "No communications channels configured" in result.output


def test_channels_list_api_failure(runner: CliRunner, mock_client: MagicMock) -> None:
    """channels list handles API errors."""
    mock_client.call_http_api.return_value = _mock_response(status_code=500, text="Server Error")

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(comms, ["channels", "list"])

    assert result.exit_code == 1
    assert "Error" in result.output


# --- comms channels add ---


def test_channels_add_telegram(runner: CliRunner, mock_client: MagicMock) -> None:
    """channels add for telegram prompts for bot_token and chat_id."""
    mock_client.call_http_api.return_value = _mock_response(status_code=201)

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(
            comms,
            ["channels", "add", "telegram", "my-tg"],
            input="fake-bot-token\n12345\n",
        )

    assert result.exit_code == 0
    assert "added successfully" in result.output

    call_args = mock_client.call_http_api.call_args
    json_data = call_args.kwargs.get("json_data", {})
    assert json_data["channel_type"] == "telegram"
    assert json_data["name"] == "my-tg"


def test_channels_add_slack(runner: CliRunner, mock_client: MagicMock) -> None:
    """channels add for slack prompts for token, signing secret, and channel."""
    mock_client.call_http_api.return_value = _mock_response(status_code=200)

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(
            comms,
            ["channels", "add", "slack", "my-slack"],
            input="xoxb-fake-token\nsigning-secret\nC123\n",
        )

    assert result.exit_code == 0
    assert "added successfully" in result.output


def test_channels_add_generic_json(runner: CliRunner, mock_client: MagicMock) -> None:
    """channels add for unknown type accepts raw JSON config."""
    mock_client.call_http_api.return_value = _mock_response(status_code=200)

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(
            comms,
            ["channels", "add", "custom", "my-custom"],
            input='{"api_key": "abc"}\n',
        )

    assert result.exit_code == 0
    assert "added successfully" in result.output


def test_channels_add_api_failure(runner: CliRunner, mock_client: MagicMock) -> None:
    """channels add handles API errors."""
    mock_client.call_http_api.return_value = _mock_response(status_code=400, text="Invalid config")

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(
            comms,
            ["channels", "add", "telegram", "my-tg"],
            input="fake-token\n\n",
        )

    assert result.exit_code == 1
    assert "Error" in result.output


# --- comms channels remove ---


def test_channels_remove_success(runner: CliRunner, mock_client: MagicMock) -> None:
    """channels remove finds channel by name and deletes it."""
    mock_client.call_http_api.side_effect = [
        # First call: GET channels list
        _mock_response(
            json_data=[
                {"id": "chan-1", "name": "my-slack"},
                {"id": "chan-2", "name": "my-telegram"},
            ]
        ),
        # Second call: DELETE channel
        _mock_response(status_code=204),
    ]

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(comms, ["channels", "remove", "my-slack", "--yes"])

    assert result.exit_code == 0
    assert "removed successfully" in result.output
    # Verify DELETE was called with correct channel ID
    delete_call = mock_client.call_http_api.call_args_list[1]
    assert "/api/comms/channels/chan-1" in delete_call[0][0]


def test_channels_remove_not_found(runner: CliRunner, mock_client: MagicMock) -> None:
    """channels remove shows error when channel name not found."""
    mock_client.call_http_api.return_value = _mock_response(json_data=[])

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(comms, ["channels", "remove", "nonexistent", "--yes"])

    assert result.exit_code == 1
    assert "not found" in result.output


def test_channels_remove_connection_failure(runner: CliRunner, mock_client: MagicMock) -> None:
    """channels remove handles daemon connection failure."""
    import httpx

    mock_client.call_http_api.side_effect = httpx.RequestError("Connection refused")

    with patch("gobby.cli.communications.get_daemon_client", return_value=mock_client):
        result = runner.invoke(comms, ["channels", "remove", "my-slack", "--yes"])

    assert result.exit_code == 1
    assert "Daemon connection failed" in result.output
