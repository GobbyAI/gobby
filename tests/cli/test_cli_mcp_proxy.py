import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.mcp_proxy import mcp_proxy
from gobby.config.app import DaemonConfig
from gobby.utils.daemon_client import DaemonClient

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_daemon_client():
    client = MagicMock(spec=DaemonClient)
    client.check_health.return_value = (True, None)
    return client


@pytest.fixture
def cli_runner(mock_daemon_client):
    runner = CliRunner()
    # Patch get_daemon_client to return our mock
    # passing obj to invoke is handled by the runner, but we need to ensure get_daemon_client uses it or is patched
    return runner


@pytest.fixture
def mock_config():
    return MagicMock(spec=DaemonConfig, daemon_port=60887)


def test_list_servers_success(cli_runner, mock_daemon_client, mock_config) -> None:
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "servers": [
                {"name": "server1", "connected": True, "state": "connected"},
                {"name": "server2", "connected": False, "state": "disconnected"},
            ],
            "connected": 1,
            "total": 2,
        }

        result = cli_runner.invoke(mcp_proxy, ["list-servers"], obj={"config": mock_config})

        assert result.exit_code == 0
        assert "MCP Servers (1/2 connected):" in result.output
        assert "● server1 (connected)" in result.output
        assert "○ server2 (disconnected)" in result.output


def test_list_servers_json(cli_runner, mock_daemon_client, mock_config) -> None:
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_response = {"servers": [{"name": "server1"}], "connected": 1, "total": 1}
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = mock_response

        result = cli_runner.invoke(
            mcp_proxy, ["list-servers", "--json"], obj={"config": mock_config}
        )

        assert result.exit_code == 0
        output_json = json.loads(result.output)
        assert output_json == mock_response


def test_list_tools_success(cli_runner, mock_daemon_client, mock_config) -> None:
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "tools": {"server1": [{"name": "tool1", "description": "A test tool"}]}
        }

        result = cli_runner.invoke(mcp_proxy, ["list-tools"], obj={"config": mock_config})

        assert result.exit_code == 0
        assert "server1:" in result.output
        assert "• tool1" in result.output
        assert "A test tool" in result.output


def test_get_schema(cli_runner, mock_daemon_client, mock_config) -> None:
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_schema = {"name": "tool1", "inputSchema": {}}
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = mock_schema

        result = cli_runner.invoke(
            mcp_proxy, ["get-schema", "server1", "tool1"], obj={"config": mock_config}
        )

        assert result.exit_code == 0
        output_json = json.loads(result.output)
        assert output_json == mock_schema

        # Verify the API was called correctly
        mock_daemon_client.call_http_api.assert_called_with(
            "/api/mcp/tools/schema",
            method="POST",
            json_data={"server_name": "server1", "tool_name": "tool1"},
            timeout=30.0,
        )


def test_call_tool_success(cli_runner, mock_daemon_client, mock_config) -> None:
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        expected_result = {"result": "success"}
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "success": True,
            "result": expected_result,
        }

        result = cli_runner.invoke(
            mcp_proxy,
            ["call-tool", "server1", "tool1", "-a", "arg1=val1"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        assert json.dumps(expected_result, indent=2) in result.output
        # Verify the API was called correctly
        mock_daemon_client.call_http_api.assert_called_with(
            "/api/mcp/tools/call",
            method="POST",
            json_data={
                "server_name": "server1",
                "tool_name": "tool1",
                "arguments": {"arg1": "val1"},
            },
            timeout=30.0,
        )


def test_daemon_not_running(cli_runner, mock_daemon_client, mock_config) -> None:
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.check_health.return_value = (False, None)

        result = cli_runner.invoke(mcp_proxy, ["list-servers"], obj={"config": mock_config})

        assert result.exit_code == 1
        assert "Gobby daemon is not running" in result.output


# ==============================================================================
# Tests for add-server command
# ==============================================================================


def test_add_server_http(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test adding an HTTP MCP server."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {"success": True}

        result = cli_runner.invoke(
            mcp_proxy,
            ["add-server", "my-http-server", "-t", "http", "-u", "https://api.example.com/mcp"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        assert "Added MCP server: my-http-server" in result.output

        # Verify the API was called with correct data
        mock_daemon_client.call_http_api.assert_called_with(
            "/api/mcp/servers",
            method="POST",
            json_data={
                "name": "my-http-server",
                "transport": "http",
                "url": "https://api.example.com/mcp",
                "command": None,
                "args": None,
                "env": None,
                "headers": None,
                "enabled": True,
            },
            timeout=30.0,
        )


def test_add_server_stdio(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test adding a stdio MCP server."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {"success": True}

        result = cli_runner.invoke(
            mcp_proxy,
            [
                "add-server",
                "my-stdio-server",
                "-t",
                "stdio",
                "-c",
                "npx",
                "--args",
                '["mcp-server"]',
            ],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        assert "Added MCP server: my-stdio-server" in result.output


def test_add_server_http_missing_url(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test adding HTTP server without URL fails."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        result = cli_runner.invoke(
            mcp_proxy,
            ["add-server", "my-server", "-t", "http"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 1
        assert "--url is required" in result.output


def test_add_server_stdio_missing_command(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test adding stdio server without command fails."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        result = cli_runner.invoke(
            mcp_proxy,
            ["add-server", "my-server", "-t", "stdio"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 1
        assert "--command is required" in result.output


def test_add_server_with_env(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test adding server with environment variables."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {"success": True}

        result = cli_runner.invoke(
            mcp_proxy,
            [
                "add-server",
                "my-server",
                "-t",
                "stdio",
                "-c",
                "node",
                "-e",
                '{"API_KEY": "secret123"}',
            ],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0


def test_add_server_disabled(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test adding server in disabled state."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {"success": True}

        result = cli_runner.invoke(
            mcp_proxy,
            ["add-server", "my-server", "-t", "http", "-u", "http://example.com", "--disabled"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0

        # Verify enabled=False was passed
        call_args = mock_daemon_client.call_http_api.call_args
        assert call_args[1]["json_data"]["enabled"] is False


def test_add_server_invalid_json_args(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test adding server with invalid JSON args fails."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        result = cli_runner.invoke(
            mcp_proxy,
            ["add-server", "my-server", "-t", "stdio", "-c", "node", "--args", "not-valid-json"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 1
        assert "Invalid JSON" in result.output


# ==============================================================================
# Tests for remove-server command
# ==============================================================================


def test_remove_server_success(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test removing an MCP server."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {"success": True}

        result = cli_runner.invoke(
            mcp_proxy,
            ["remove-server", "old-server", "--yes"],  # --yes to skip confirmation
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        assert "Removed MCP server: old-server" in result.output


def test_remove_server_failure(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test remove server failure."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "success": False,
            "error": "Server not found",
        }

        result = cli_runner.invoke(
            mcp_proxy,
            ["remove-server", "nonexistent", "--yes"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 1
        assert "Server not found" in result.output


# ==============================================================================
# Tests for recommend-tools command
# ==============================================================================


def test_recommend_tools_success(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test getting tool recommendations."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "recommendations": [
                {
                    "server": "supabase",
                    "tool": "list_tables",
                    "reason": "Best for database queries",
                },
                {
                    "server": "context7",
                    "tool": "get-library-docs",
                    "reason": "Good for documentation",
                },
            ]
        }

        result = cli_runner.invoke(
            mcp_proxy,
            ["recommend-tools", "I need to query a database"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        assert "Recommended tools:" in result.output
        assert "supabase/list_tables" in result.output
        assert "Best for database queries" in result.output


def test_recommend_tools_no_results(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test recommend-tools with no results."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {"recommendations": []}

        result = cli_runner.invoke(
            mcp_proxy,
            ["recommend-tools", "something very obscure"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        assert "No tool recommendations found" in result.output


def test_recommend_tools_json_format(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test recommend-tools with JSON output."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_response = {
            "recommendations": [{"server": "test", "tool": "test_tool", "reason": "test"}]
        }
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = mock_response

        result = cli_runner.invoke(
            mcp_proxy,
            ["recommend-tools", "test query", "--json"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        output_json = json.loads(result.output)
        assert output_json == mock_response


def test_recommend_tools_with_agent(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test recommend-tools filtered by agent."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {"recommendations": []}

        result = cli_runner.invoke(
            mcp_proxy,
            ["recommend-tools", "query", "--agent", "my-agent"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0

        # Verify agent_id was passed
        call_args = mock_daemon_client.call_http_api.call_args
        assert call_args[1]["json_data"]["agent_id"] == "my-agent"


# ==============================================================================
# Tests for search-tools command
# ==============================================================================


def test_search_tools_success(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test searching for tools."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "results": [
                {
                    "server_name": "supabase",
                    "tool_name": "execute_sql",
                    "similarity": 0.95,
                    "description": "Execute SQL queries",
                }
            ]
        }

        result = cli_runner.invoke(
            mcp_proxy,
            ["search-tools", "execute sql queries"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        assert "supabase/execute_sql" in result.output
        assert "95.00%" in result.output


def test_search_tools_no_results(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test search-tools with no results."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {"results": []}

        result = cli_runner.invoke(
            mcp_proxy,
            ["search-tools", "xyz123nonexistent"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        assert "No matching tools found" in result.output


def test_search_tools_with_options(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test search-tools with custom options."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {"results": []}

        result = cli_runner.invoke(
            mcp_proxy,
            [
                "search-tools",
                "query",
                "--top-k",
                "5",
                "--min-similarity",
                "0.5",
                "--server",
                "supabase",
            ],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0

        # Verify options were passed
        call_args = mock_daemon_client.call_http_api.call_args
        assert call_args[1]["json_data"]["top_k"] == 5
        assert call_args[1]["json_data"]["min_similarity"] == 0.5
        assert call_args[1]["json_data"]["server"] == "supabase"


# ==============================================================================
# Tests for import-server command
# ==============================================================================


def test_import_server_from_project(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test importing servers from another project."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "success": True,
            "imported": ["server1", "server2"],
            "skipped": [],
            "failed": [],
        }

        result = cli_runner.invoke(
            mcp_proxy,
            ["import-server", "--from-project", "other-project"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        assert "Imported 2 server(s)" in result.output


def test_import_server_needs_configuration(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test import that needs additional configuration."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "status": "needs_configuration",
            "config": {"name": "my-server", "transport": "stdio"},
            "missing": ["API_KEY"],
            "instructions": "Set API_KEY in your environment",
        }

        result = cli_runner.invoke(
            mcp_proxy,
            ["import-server", "--github", "https://github.com/example/mcp-server"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        assert "needs secrets" in result.output
        assert "API_KEY" in result.output


def test_import_server_no_source(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test import without specifying a source."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        result = cli_runner.invoke(
            mcp_proxy,
            ["import-server"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 1
        assert "Specify at least one source" in result.output


def test_import_server_with_specific_servers(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test importing specific servers."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "success": True,
            "imported": ["server1"],
            "skipped": [],
            "failed": [],
        }

        result = cli_runner.invoke(
            mcp_proxy,
            ["import-server", "--from-project", "prod", "-s", "server1", "-s", "server2"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0


# ==============================================================================
# Tests for refresh command
# ==============================================================================


def test_refresh_success(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test refreshing MCP tools."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "success": True,
            "stats": {
                "servers_processed": 3,
                "tools_new": 5,
                "tools_changed": 2,
                "tools_unchanged": 10,
                "tools_removed": 1,
                "embeddings_generated": 7,
            },
        }

        result = cli_runner.invoke(
            mcp_proxy,
            ["refresh"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        assert "MCP Tools Refresh Complete" in result.output
        assert "Servers processed: 3" in result.output
        assert "New tools: 5" in result.output


def test_refresh_with_force(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test refresh with --force flag."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "success": True,
            "stats": {
                "servers_processed": 1,
                "tools_new": 10,
                "tools_changed": 0,
                "tools_unchanged": 0,
                "tools_removed": 0,
                "embeddings_generated": 10,
            },
        }

        result = cli_runner.invoke(
            mcp_proxy,
            ["refresh", "--force"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        assert "--force: all tools treated as new" in result.output


def test_refresh_specific_server(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test refresh for specific server."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "success": True,
            "stats": {
                "servers_processed": 1,
                "tools_new": 0,
                "tools_changed": 0,
                "tools_unchanged": 5,
                "tools_removed": 0,
                "embeddings_generated": 0,
            },
        }

        result = cli_runner.invoke(
            mcp_proxy,
            ["refresh", "--server", "context7"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        # Verify server filter was passed
        call_args = mock_daemon_client.call_http_api.call_args
        assert call_args[1]["json_data"]["server"] == "context7"


def test_refresh_failure(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test refresh failure."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "success": False,
            "error": "Connection timeout",
        }

        result = cli_runner.invoke(
            mcp_proxy,
            ["refresh"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 1
        assert "Connection timeout" in result.output


# ==============================================================================
# Tests for status command
# ==============================================================================


def test_status_success(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test MCP proxy status."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "total_servers": 5,
            "connected_servers": 3,
            "cached_tools": 47,
            "server_health": {
                "context7": {"state": "connected", "health": "healthy", "failures": 0},
                "supabase": {"state": "disconnected", "health": "unhealthy", "failures": 3},
            },
        }

        result = cli_runner.invoke(
            mcp_proxy,
            ["status"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        assert "MCP Proxy Status" in result.output
        assert "Servers: 5" in result.output
        assert "Connected: 3" in result.output
        assert "context7" in result.output


def test_status_json_format(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test status with JSON output."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_response = {
            "total_servers": 2,
            "connected_servers": 1,
            "cached_tools": 10,
            "server_health": {},
        }
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = mock_response

        result = cli_runner.invoke(
            mcp_proxy,
            ["status", "--json"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        output_json = json.loads(result.output)
        assert output_json == mock_response


# ==============================================================================
# Tests for list-tools with server filter
# ==============================================================================


def test_list_tools_with_server_filter(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test list-tools with server filter."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "tools": {"context7": [{"name": "get-library-docs", "description": "Get docs"}]}
        }

        result = cli_runner.invoke(
            mcp_proxy, ["list-tools", "--server", "context7"], obj={"config": mock_config}
        )

        assert result.exit_code == 0
        assert "context7:" in result.output
        assert "get-library-docs" in result.output


def test_list_tools_no_tools(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test list-tools with no tools available."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {"tools": {}}

        result = cli_runner.invoke(mcp_proxy, ["list-tools"], obj={"config": mock_config})

        assert result.exit_code == 0
        assert "No tools available" in result.output


# ==============================================================================
# Tests for call-tool with JSON args
# ==============================================================================


def test_call_tool_with_json_args(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test call-tool with JSON arguments."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "success": True,
            "result": {"data": "test"},
        }

        result = cli_runner.invoke(
            mcp_proxy,
            ["call-tool", "server1", "tool1", "-j", '{"key": "value", "count": 5}'],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0

        # Verify arguments were parsed correctly
        call_args = mock_daemon_client.call_http_api.call_args
        assert call_args[1]["json_data"]["arguments"] == {"key": "value", "count": 5}


def test_call_tool_invalid_json_args(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test call-tool with invalid JSON arguments."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        result = cli_runner.invoke(
            mcp_proxy,
            ["call-tool", "server1", "tool1", "-j", "not-valid-json"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 1
        assert "Invalid JSON arguments" in result.output


def test_call_tool_invalid_arg_format(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test call-tool with invalid arg format."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        result = cli_runner.invoke(
            mcp_proxy,
            ["call-tool", "server1", "tool1", "-a", "no-equals-sign"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 1
        assert "Invalid argument format" in result.output


def test_call_tool_raw_output(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test call-tool with raw output."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_response = {"success": True, "result": {"nested": {"data": "value"}}}
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = mock_response

        result = cli_runner.invoke(
            mcp_proxy,
            ["call-tool", "server1", "tool1", "--raw"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 0
        output_json = json.loads(result.output)
        assert output_json == mock_response


def test_call_tool_failure(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test call-tool with tool execution failure."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 200
        mock_daemon_client.call_http_api.return_value.json.return_value = {
            "success": False,
            "error": "Tool execution failed",
        }

        result = cli_runner.invoke(
            mcp_proxy,
            ["call-tool", "server1", "tool1"],
            obj={"config": mock_config},
        )

        assert result.exit_code == 1
        assert "Tool execution failed" in result.output


# ==============================================================================
# Tests for error handling
# ==============================================================================


def test_daemon_connection_error(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test handling of connection errors."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.check_health.return_value = (False, "Connection refused")

        result = cli_runner.invoke(mcp_proxy, ["list-servers"], obj={"config": mock_config})

        assert result.exit_code == 1
        assert "Connection refused" in result.output


def test_api_error_response(cli_runner, mock_daemon_client, mock_config) -> None:
    """Test handling of API error responses."""
    with patch("gobby.cli.mcp_proxy.get_daemon_client", return_value=mock_daemon_client):
        mock_daemon_client.call_http_api.return_value.status_code = 500
        mock_daemon_client.call_http_api.return_value.text = "Internal Server Error"

        result = cli_runner.invoke(mcp_proxy, ["list-servers"], obj={"config": mock_config})

        assert result.exit_code == 1
        assert "Internal Server Error" in result.output
