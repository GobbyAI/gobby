"""The CLI requests a link with installed credentials; it accepts no password."""

from unittest.mock import patch

import httpx
from click.testing import CliRunner

from gobby.cli import cli


def test_sign_in_link_uses_daemon_client() -> None:
    link = "http://127.0.0.1:43210/api/auth/sign-in?token=one-use-test-token"
    response = httpx.Response(200, json={"url": link, "expires_in": 120})
    with patch(
        "gobby.utils.daemon_client.DaemonClient.call_http_api", return_value=response
    ) as call:
        result = CliRunner().invoke(cli, ["ui", "sign-in-link"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == link
    call.assert_called_once_with("/api/auth/sign-in-link")
