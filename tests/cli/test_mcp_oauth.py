"""CLI OAuth command scope, success, and failure behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.mcp_proxy import mcp_proxy

pytestmark = pytest.mark.unit


def test_add_server_oauth_flag_reaches_api() -> None:
    with (
        patch("gobby.cli.mcp_proxy.get_daemon_client"),
        patch("gobby.cli.mcp_proxy.check_daemon_running", return_value=True),
        patch(
            "gobby.cli.mcp_proxy.resolve_cli_mcp_project", return_value=("project-id", "project")
        ),
        patch("gobby.cli.mcp_proxy.call_mcp_api", return_value={"success": True}) as api,
    ):
        result = CliRunner().invoke(
            mcp_proxy,
            [
                "add-server",
                "fieldy",
                "--transport",
                "http",
                "--url",
                "https://api.fieldy.ai/mcp",
                "--oauth",
            ],
        )
    assert result.exit_code == 0, result.output
    assert api.call_args.kwargs["json_data"]["requires_oauth"] is True


@pytest.mark.parametrize("global_scope", [False, True])
def test_auth_enables_oauth_only_after_success(global_scope: bool) -> None:
    row = SimpleNamespace(
        name="fieldy",
        id="instance-id",
        project_id="project-id",
        transport="http",
        url="https://api.fieldy.ai/mcp",
        headers=None,
    )
    scope = "global" if global_scope else "project"
    project_id = None if global_scope else "project-id"
    database = MagicMock()
    with (
        patch("gobby.cli.mcp_oauth.get_daemon_client"),
        patch("gobby.cli.mcp_oauth.check_daemon_running", return_value=True),
        patch("gobby.cli.mcp_oauth.resolve_cli_mcp_project", return_value=(project_id, scope)),
        patch("gobby.cli.mcp_oauth.require_cli_database", return_value=database),
        patch("gobby.cli.mcp_oauth.LocalMCPManager") as registry,
        patch("gobby.cli.mcp_oauth.SecretStore"),
        patch("gobby.cli.mcp_oauth.authorize_server", new_callable=AsyncMock) as authorize,
        patch("gobby.cli.mcp_oauth.call_mcp_api", return_value={"success": True}) as api,
    ):
        registry.return_value.get_server.return_value = row
        result = CliRunner().invoke(
            mcp_proxy, ["auth", "fieldy"] + (["--global"] if global_scope else [])
        )
    assert result.exit_code == 0, result.output
    assert "Authorized MCP server: fieldy" in result.output
    assert authorize.await_args is not None
    assert authorize.await_args.args[0].id == "instance-id"
    payload = api.call_args.kwargs["json_data"]
    assert payload["requires_oauth"] is True and payload["scope"] == scope
    assert payload.get("project_id") == project_id


def test_auth_failure_does_not_enable_oauth_or_expose_error_body() -> None:
    row = SimpleNamespace(
        name="fieldy",
        id="instance-id",
        project_id="project-id",
        transport="http",
        url="https://api.fieldy.ai/mcp",
        headers=None,
    )
    with (
        patch("gobby.cli.mcp_oauth.get_daemon_client"),
        patch("gobby.cli.mcp_oauth.check_daemon_running", return_value=True),
        patch(
            "gobby.cli.mcp_oauth.resolve_cli_mcp_project", return_value=("project-id", "project")
        ),
        patch("gobby.cli.mcp_oauth.require_cli_database"),
        patch("gobby.cli.mcp_oauth.LocalMCPManager") as registry,
        patch("gobby.cli.mcp_oauth.SecretStore"),
        patch("gobby.cli.mcp_oauth.authorize_server", new_callable=AsyncMock) as authorize,
        patch("gobby.cli.mcp_oauth.call_mcp_api") as api,
    ):
        registry.return_value.get_server.return_value = row
        authorize.side_effect = RuntimeError("private-token-response")
        result = CliRunner().invoke(mcp_proxy, ["auth", "fieldy"])
    assert result.exit_code == 1
    assert "private-token-response" not in result.output
    assert "OAuth authorization failed" in result.output
    api.assert_not_called()
