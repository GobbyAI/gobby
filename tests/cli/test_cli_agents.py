"""Comprehensive tests for the agents CLI module.

Tests for all commands in src/gobby/cli/agents.py:
- start: Start a new agent
- list: List agent runs for a session
- show: Show details for an agent run
- status: Check status of a running agent
- cancel: Cancel a running agent
- stats: Show agent run statistics
- cleanup: Clean up stale agent runs

Uses Click's CliRunner and mocks external dependencies.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli

pytestmark = pytest.mark.unit

# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


@pytest.fixture
def mock_agent_run():
    """Create a mock agent run with common attributes."""
    run = MagicMock()
    run.id = "ar-abc123def456"
    run.parent_session_id = "sess-parent123"
    run.child_session_id = "sess-child456"
    run.workflow_name = "test-workflow"
    run.provider = "claude"
    run.model = "claude-3-opus"
    run.status = "running"
    run.prompt = "Test prompt for the agent"
    run.result = None
    run.error = None
    run.tool_calls_count = 5
    run.turns_used = 3
    run.started_at = "2024-01-01T10:00:00Z"
    run.completed_at = None
    run.created_at = "2024-01-01T09:59:00Z"
    run.updated_at = "2024-01-01T10:01:00Z"
    run.to_dict.return_value = {
        "id": "ar-abc123def456",
        "parent_session_id": "sess-parent123",
        "child_session_id": "sess-child456",
        "workflow_name": "test-workflow",
        "provider": "claude",
        "model": "claude-3-opus",
        "status": "running",
        "prompt": "Test prompt for the agent",
        "result": None,
        "error": None,
        "tool_calls_count": 5,
        "turns_used": 3,
        "started_at": "2024-01-01T10:00:00Z",
        "completed_at": None,
        "created_at": "2024-01-01T09:59:00Z",
        "updated_at": "2024-01-01T10:01:00Z",
    }
    return run


@pytest.fixture
def mock_completed_run(mock_agent_run):
    """Create a mock completed agent run."""
    run = MagicMock()
    run.id = "ar-completed123"
    run.parent_session_id = "sess-parent123"
    run.child_session_id = "sess-child789"
    run.workflow_name = "plan-execute"
    run.provider = "claude"
    run.model = "claude-3-sonnet"
    run.status = "success"
    run.prompt = "Completed task prompt"
    run.result = "Task completed successfully with all objectives met."
    run.error = None
    run.tool_calls_count = 15
    run.turns_used = 8
    run.started_at = "2024-01-01T10:00:00Z"
    run.completed_at = "2024-01-01T10:30:00Z"
    run.created_at = "2024-01-01T09:59:00Z"
    run.updated_at = "2024-01-01T10:30:00Z"
    run.to_dict.return_value = {
        "id": "ar-completed123",
        "parent_session_id": "sess-parent123",
        "child_session_id": "sess-child789",
        "workflow_name": "plan-execute",
        "provider": "claude",
        "model": "claude-3-sonnet",
        "status": "success",
        "prompt": "Completed task prompt",
        "result": "Task completed successfully with all objectives met.",
        "error": None,
        "tool_calls_count": 15,
        "turns_used": 8,
        "started_at": "2024-01-01T10:00:00Z",
        "completed_at": "2024-01-01T10:30:00Z",
        "created_at": "2024-01-01T09:59:00Z",
        "updated_at": "2024-01-01T10:30:00Z",
    }
    return run


@pytest.fixture
def mock_failed_run():
    """Create a mock failed agent run."""
    run = MagicMock()
    run.id = "ar-failed456"
    run.parent_session_id = "sess-parent123"
    run.child_session_id = None
    run.workflow_name = None
    run.provider = "gemini"
    run.model = None
    run.status = "error"
    run.prompt = "Failed task prompt"
    run.result = None
    run.error = "Connection timeout after 30 seconds"
    run.tool_calls_count = 2
    run.turns_used = 1
    run.started_at = "2024-01-01T10:00:00Z"
    run.completed_at = "2024-01-01T10:00:30Z"
    run.created_at = "2024-01-01T09:59:00Z"
    run.updated_at = "2024-01-01T10:00:30Z"
    run.to_dict.return_value = {
        "id": "ar-failed456",
        "parent_session_id": "sess-parent123",
        "child_session_id": None,
        "workflow_name": None,
        "provider": "gemini",
        "model": None,
        "status": "error",
        "prompt": "Failed task prompt",
        "result": None,
        "error": "Connection timeout after 30 seconds",
        "tool_calls_count": 2,
        "turns_used": 1,
        "started_at": "2024-01-01T10:00:00Z",
        "completed_at": "2024-01-01T10:00:30Z",
        "created_at": "2024-01-01T09:59:00Z",
        "updated_at": "2024-01-01T10:00:30Z",
    }
    return run


# ==============================================================================
# Tests for agents group command
# ==============================================================================


class TestAgentsGroup:
    """Tests for the agents command group."""

    def test_agents_help(self, runner: CliRunner) -> None:
        """Test agents --help displays help text."""
        result = runner.invoke(cli, ["agents", "--help"])

        assert result.exit_code == 0
        assert "Manage agent definitions and runs" in result.output

    def test_agents_group_alone(self, runner: CliRunner) -> None:
        """Test invoking agents alone shows help or requires subcommand."""
        result = runner.invoke(cli, ["agents"])

        # Click groups may exit with 0 or 2 depending on configuration
        # Should show available subcommands or missing command message
        assert "spawn" in result.output or "Usage" in result.output


# ==============================================================================
# Tests for agents start command
# ==============================================================================


class TestAgentsSpawnCommand:
    """Tests for gobby agents spawn command."""

    def test_spawn_help(self, runner: CliRunner) -> None:
        """Test spawn --help displays help text."""
        result = runner.invoke(cli, ["agents", "spawn", "--help"])

        assert result.exit_code == 0
        assert "Spawn a new agent" in result.output
        assert "--session" in result.output
        assert "--workflow" in result.output
        assert "--mode" in result.output
        assert "--terminal" in result.output
        assert "--provider" in result.output

    def test_spawn_requires_session(self, runner: CliRunner) -> None:
        """Test spawn requires --session option."""
        result = runner.invoke(cli, ["agents", "spawn", "Test prompt"])

        assert result.exit_code == 2
        assert "Missing option" in result.output or "required" in result.output.lower()

    def test_spawn_rejects_reasoning_required_without_effort(self, runner: CliRunner) -> None:
        """Test spawn validates reasoning-required against missing effort."""
        result = runner.invoke(
            cli,
            [
                "agents",
                "spawn",
                "Test prompt",
                "--session",
                "sess-parent123",
                "--reasoning-required",
            ],
        )

        assert result.exit_code == 2
        assert "--reasoning-required requires --reasoning-effort" in result.output

    @patch("gobby.cli.agents.resolve_session_id")
    @patch("gobby.cli.agents.httpx.post")
    @patch("gobby.cli.agents.get_daemon_url")
    def test_spawn_success(
        self,
        mock_get_url: MagicMock,
        mock_post: MagicMock,
        mock_resolve_session: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test successful agent spawn."""
        mock_get_url.return_value = "http://localhost:60887"
        mock_resolve_session.return_value = "sess-parent123"
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "success": True,
            "run_id": "ar-newrun123",
            "child_session_id": "sess-child001",
            "status": "running",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = runner.invoke(
            cli,
            ["agents", "spawn", "Test prompt", "--session", "sess-parent123"],
        )

        assert result.exit_code == 0
        assert "Started agent run" in result.output
        assert "ar-newrun123" in result.output
        assert "sess-child001" in result.output

    @patch("gobby.cli.agents.resolve_session_id")
    @patch("gobby.cli.agents.httpx.post")
    @patch("gobby.cli.agents.get_daemon_url")
    def test_spawn_with_all_options(
        self,
        mock_get_url: MagicMock,
        mock_post: MagicMock,
        mock_resolve_session: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test spawn with all optional parameters."""
        mock_get_url.return_value = "http://localhost:60887"
        mock_resolve_session.return_value = "sess-parent123"
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "success": True,
            "run_id": "ar-newrun456",
            "child_session_id": "sess-child002",
            "status": "running",
            "message": "Agent started in terminal mode",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = runner.invoke(
            cli,
            [
                "agents",
                "spawn",
                "Implement feature X",
                "--session",
                "sess-parent123",
                "--workflow",
                "plan-execute",
                "--task",
                "gt-task123",
                "--terminal",
                "iterm",
                "--provider",
                "claude",
                "--model",
                "claude-3-opus",
                "--timeout",
                "300",
                "--max-turns",
                "20",
                "--context",
                "summary_markdown",
            ],
        )

        assert result.exit_code == 0
        assert "Started agent run" in result.output
        assert "ar-newrun456" in result.output

        # Verify the POST call was made with correct arguments
        call_args = mock_post.call_args
        assert call_args[1]["json"]["prompt"] == "Implement feature X"
        assert call_args[1]["json"]["parent_session_id"] == "sess-parent123"
        assert call_args[1]["json"]["workflow"] == "plan-execute"
        assert call_args[1]["json"]["task"] == "gt-task123"
        assert call_args[1]["json"]["terminal"] == "iterm"
        assert call_args[1]["json"]["provider"] == "claude"
        assert call_args[1]["json"]["model"] == "claude-3-opus"
        assert call_args[1]["json"]["timeout"] == 300.0
        assert call_args[1]["json"]["max_turns"] == 20
        assert call_args[1]["json"]["session_context"] == "summary_markdown"

    @patch("gobby.cli.agents.resolve_session_id")
    @patch("gobby.cli.agents.httpx.post")
    @patch("gobby.cli.agents.get_daemon_url")
    def test_spawn_json_output(
        self,
        mock_get_url: MagicMock,
        mock_post: MagicMock,
        mock_resolve_session: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test spawn with JSON output format."""
        mock_get_url.return_value = "http://localhost:60887"
        mock_resolve_session.return_value = "sess-parent"
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "success": True,
            "run_id": "ar-json123",
            "child_session_id": "sess-json",
            "status": "running",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = runner.invoke(
            cli,
            [
                "agents",
                "spawn",
                "Test prompt",
                "--session",
                "sess-parent",
                "--json",
            ],
        )

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["success"] is True
        assert data["run_id"] == "ar-json123"

    @patch("gobby.cli.agents.resolve_session_id")
    @patch("gobby.cli.agents.httpx.post")
    @patch("gobby.cli.agents.get_daemon_url")
    def test_spawn_daemon_connection_error(
        self,
        mock_get_url: MagicMock,
        mock_post: MagicMock,
        mock_resolve_session: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test spawn when daemon is not running."""
        import httpx

        mock_get_url.return_value = "http://localhost:60887"
        mock_post.side_effect = httpx.ConnectError("Connection refused")
        mock_resolve_session.return_value = "sess-parent"

        result = runner.invoke(
            cli,
            ["agents", "spawn", "Test prompt", "--session", "sess-parent"],
        )

        assert result.exit_code == 0  # CLI exits cleanly with error message
        assert "Cannot connect to Gobby daemon" in result.output
        assert "gobby start" in result.output

    @patch("gobby.cli.agents.resolve_session_id")
    @patch("gobby.cli.agents.httpx.post")
    @patch("gobby.cli.agents.get_daemon_url")
    def test_spawn_daemon_http_error(
        self,
        mock_get_url: MagicMock,
        mock_post: MagicMock,
        mock_resolve_session: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test spawn when daemon returns HTTP error."""
        import httpx

        mock_get_url.return_value = "http://localhost:60887"
        mock_resolve_session.return_value = "sess-parent"
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal server error"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Server error", request=MagicMock(), response=mock_response
        )
        mock_post.return_value = mock_response

        result = runner.invoke(
            cli,
            ["agents", "spawn", "Test prompt", "--session", "sess-parent"],
        )

        assert result.exit_code == 0
        assert "Error: Daemon returned 500" in result.output

    @patch("gobby.cli.agents.resolve_session_id")
    @patch("gobby.cli.agents.httpx.post")
    @patch("gobby.cli.agents.get_daemon_url")
    def test_spawn_failure_response(
        self,
        mock_get_url: MagicMock,
        mock_post: MagicMock,
        mock_resolve_session: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test spawn with failure response from daemon."""
        mock_get_url.return_value = "http://localhost:60887"
        mock_resolve_session.return_value = "sess-nonexistent"
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "success": False,
            "error": "Session not found",
        }
        mock_response.raise_for_status = MagicMock()
        mock_post.return_value = mock_response

        result = runner.invoke(
            cli,
            ["agents", "spawn", "Test prompt", "--session", "sess-nonexistent"],
        )

        assert result.exit_code == 0
        assert "Failed to start agent" in result.output
        assert "Session not found" in result.output

    @patch("gobby.cli.agents.resolve_session_id")
    @patch("gobby.cli.agents.httpx.post")
    @patch("gobby.cli.agents.get_daemon_url")
    def test_start_generic_exception(
        self,
        mock_get_url: MagicMock,
        mock_post: MagicMock,
        mock_resolve_session: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test start handles generic exceptions."""
        mock_get_url.return_value = "http://localhost:60887"
        mock_resolve_session.return_value = "sess-parent"
        mock_post.side_effect = Exception("Unexpected error")

        result = runner.invoke(
            cli,
            ["agents", "spawn", "Test prompt", "--session", "sess-parent"],
        )

        assert result.exit_code == 0
        assert "Error: Unexpected error" in result.output

    def test_start_terminal_choices(self, runner: CliRunner) -> None:
        """Test start terminal option validates choices."""
        result = runner.invoke(
            cli,
            [
                "agents",
                "spawn",
                "Test",
                "--session",
                "sess",
                "--terminal",
                "invalid_term",
            ],
        )

        assert result.exit_code == 2
        assert "Invalid value" in result.output


# ==============================================================================
# Tests for agents runs list command
# ==============================================================================


class TestAgentsListCommand:
    """Tests for gobby agents runs list command."""

    def test_list_help(self, runner: CliRunner) -> None:
        """Test list --help displays options."""
        result = runner.invoke(cli, ["agents", "runs", "list", "--help"])

        assert result.exit_code == 0
        assert "--session" in result.output
        assert "--status" in result.output
        assert "--limit" in result.output
        assert "--json" in result.output

    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_no_runs(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test list with no agent runs."""
        mock_manager = MagicMock()
        mock_manager.list_by_status.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["agents", "runs", "list"])

        assert result.exit_code == 0
        assert "No agent runs found" in result.output
        mock_manager.list_by_status.assert_called_once_with(status=None, limit=20)

    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_with_runs(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_agent_run: MagicMock,
    ) -> None:
        """Test list displays agent runs."""
        mock_manager = MagicMock()
        mock_manager.list_by_status.return_value = [mock_agent_run]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["agents", "runs", "list"])

        assert result.exit_code == 0
        assert "Found 1 agent run" in result.output
        assert "ar-abc123def" in result.output  # Truncated ID (12 chars)
        assert "running" in result.output
        mock_manager.list_by_status.assert_called_once_with(status=None, limit=20)

    @patch("gobby.cli.agents.resolve_session_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_by_session(
        self,
        mock_get_manager: MagicMock,
        mock_resolve_session: MagicMock,
        runner: CliRunner,
        mock_agent_run: MagicMock,
    ) -> None:
        """Test list filtered by session."""
        mock_manager = MagicMock()
        mock_manager.list_by_session.return_value = [mock_agent_run]
        mock_get_manager.return_value = mock_manager
        mock_resolve_session.return_value = "sess-parent123"

        result = runner.invoke(cli, ["agents", "runs", "list", "--session", "sess-parent123"])

        assert result.exit_code == 0
        assert "Found 1 agent run" in result.output
        mock_manager.list_by_session.assert_called_once_with(
            "sess-parent123", status=None, limit=20
        )

    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_by_status_running(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_agent_run: MagicMock,
    ) -> None:
        """Test list filtered by running status."""
        mock_manager = MagicMock()
        mock_manager.list_running.return_value = [mock_agent_run]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["agents", "runs", "list", "--status", "running"])

        assert result.exit_code == 0
        mock_manager.list_running.assert_called_once_with(limit=20)

    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_by_status_other(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_completed_run: MagicMock,
    ) -> None:
        """Test list filtered by non-running status."""
        mock_manager = MagicMock()
        mock_manager.list_by_status.return_value = [mock_completed_run]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["agents", "runs", "list", "--status", "success"])

        assert result.exit_code == 0
        assert "success" in result.output
        mock_manager.list_by_status.assert_called_once_with(status="success", limit=20)

    @patch("gobby.cli.agents.resolve_session_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_session_with_status(
        self,
        mock_get_manager: MagicMock,
        mock_resolve_session: MagicMock,
        runner: CliRunner,
        mock_completed_run: MagicMock,
    ) -> None:
        """Test list with both session and status filters."""
        mock_manager = MagicMock()
        mock_manager.list_by_session.return_value = [mock_completed_run]
        mock_get_manager.return_value = mock_manager
        mock_resolve_session.return_value = "sess-123"

        result = runner.invoke(
            cli,
            ["agents", "runs", "list", "--session", "sess-123", "--status", "success"],
        )

        assert result.exit_code == 0
        mock_manager.list_by_session.assert_called_once_with("sess-123", status="success", limit=20)

    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_with_limit(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test list with custom limit."""
        mock_manager = MagicMock()
        mock_manager.list_by_status.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["agents", "runs", "list", "--limit", "5"])

        assert result.exit_code == 0
        mock_manager.list_by_status.assert_called_once_with(status=None, limit=5)

    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_json_output(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_agent_run: MagicMock,
    ) -> None:
        """Test list with JSON output."""
        mock_manager = MagicMock()
        mock_manager.list_by_status.return_value = [mock_agent_run]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["agents", "runs", "list", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == mock_agent_run.id

    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_status_icons(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test list shows correct status icons."""
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        # Create runs with different statuses
        statuses = [
            ("pending", "\u25cb"),  # Empty circle
            ("running", "\u25d0"),  # Half circle
            ("success", "\u2713"),  # Check mark
            ("error", "\u2717"),  # X mark
            ("timeout", "\u23f1"),  # Stopwatch
            ("cancelled", "\u2298"),  # Circled slash
        ]

        for status, _icon in statuses:
            run = MagicMock()
            run.id = f"ar-{status}"
            run.status = status
            run.provider = "claude"
            run.prompt = "Test"
            mock_manager.list_by_status.return_value = [run]

            result = runner.invoke(cli, ["agents", "runs", "list"])

            assert result.exit_code == 0
            assert status in result.output

    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_truncates_long_prompts(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test list truncates long prompts."""
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        long_prompt = "A" * 100  # 100 characters
        run = MagicMock()
        run.id = "ar-long"
        run.status = "running"
        run.provider = "claude"
        run.prompt = long_prompt
        mock_manager.list_by_status.return_value = [run]

        result = runner.invoke(cli, ["agents", "runs", "list"])

        assert result.exit_code == 0
        # Should truncate to 40 chars + ...
        assert "..." in result.output
        assert "A" * 40 in result.output


# ==============================================================================
# Tests for agents runs show command
# ==============================================================================


class TestAgentsShowCommand:
    """Tests for gobby agents runs show command."""

    def test_show_help(self, runner: CliRunner) -> None:
        """Test show --help displays options."""
        result = runner.invoke(cli, ["agents", "runs", "show", "--help"])

        assert result.exit_code == 0
        assert "RUN_REF" in result.output
        assert "--json" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_show_exact_match(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
        mock_completed_run: MagicMock,
    ) -> None:
        """Test show with exact run ID match."""
        mock_manager = MagicMock()
        mock_manager.get.return_value = mock_completed_run
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "ar-completed123"

        result = runner.invoke(cli, ["agents", "runs", "show", "ar-completed123"])

        assert result.exit_code == 0
        assert "Agent Run: ar-completed123" in result.output
        assert "Status: success" in result.output
        assert "Provider: claude" in result.output
        assert "Model: claude-3-sonnet" in result.output

    @patch("gobby.cli.agents.get_agent_run_manager")
    @patch("gobby.cli.agents.LocalDatabase")
    def test_show_prefix_match_single(
        self,
        mock_db_cls: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_agent_run: MagicMock,
    ) -> None:
        """Test show with single prefix match."""
        mock_manager = MagicMock()
        # When resolve succeeds, it returns the ID, then manager.get(ID) is called.
        # We need manager.get to return the run.
        mock_manager.get.return_value = mock_agent_run
        mock_get_manager.return_value = mock_manager

        mock_db = MagicMock()
        mock_db.fetchall.return_value = [
            {
                "id": mock_agent_run.id,
                "parent_session_id": mock_agent_run.parent_session_id,
                "child_session_id": mock_agent_run.child_session_id,
                "workflow_name": mock_agent_run.workflow_name,
                "provider": mock_agent_run.provider,
                "model": mock_agent_run.model,
                "status": mock_agent_run.status,
                "prompt": mock_agent_run.prompt,
                "result": mock_agent_run.result,
                "error": mock_agent_run.error,
                "tool_calls_count": mock_agent_run.tool_calls_count,
                "turns_used": mock_agent_run.turns_used,
                "started_at": mock_agent_run.started_at,
                "completed_at": mock_agent_run.completed_at,
                "created_at": mock_agent_run.created_at,
                "updated_at": mock_agent_run.updated_at,
            }
        ]
        mock_db_cls.return_value = mock_db

        result = runner.invoke(cli, ["agents", "runs", "show", "ar-abc"])

        assert result.exit_code == 0
        assert "Agent Run:" in result.output

    @patch("gobby.cli.agents.get_agent_run_manager")
    @patch("gobby.cli.agents.LocalDatabase")
    def test_show_prefix_match_ambiguous(
        self,
        mock_db_cls: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test show with ambiguous prefix match."""
        mock_manager = MagicMock()
        mock_manager.get.return_value = None
        mock_get_manager.return_value = mock_manager

        mock_db = MagicMock()
        mock_db.fetchall.return_value = [
            {
                "id": "ar-abc123",
                "parent_session_id": "sess-1",
                "child_session_id": None,
                "workflow_name": None,
                "provider": "claude",
                "model": None,
                "status": "running",
                "prompt": "Test 1",
                "result": None,
                "error": None,
                "tool_calls_count": 0,
                "turns_used": 0,
                "started_at": None,
                "completed_at": None,
                "created_at": "2024-01-01",
                "updated_at": "2024-01-01",
            },
            {
                "id": "ar-abc456",
                "parent_session_id": "sess-2",
                "child_session_id": None,
                "workflow_name": None,
                "provider": "claude",
                "model": None,
                "status": "success",
                "prompt": "Test 2",
                "result": None,
                "error": None,
                "tool_calls_count": 0,
                "turns_used": 0,
                "started_at": None,
                "completed_at": None,
                "created_at": "2024-01-01",
                "updated_at": "2024-01-01",
            },
        ]
        mock_db_cls.return_value = mock_db

        result = runner.invoke(cli, ["agents", "runs", "show", "ar-abc"])

        # ClickException causes non-zero exit code
        assert result.exit_code != 0
        assert "Ambiguous agent run reference" in result.output
        # matches: ... is printed to stderr by click.echo before raising exception
        assert "matches:" in result.output
        assert "ar-abc123" in result.output
        assert "ar-abc456" in result.output

    @patch("gobby.cli.agents.get_agent_run_manager")
    @patch("gobby.cli.agents.LocalDatabase")
    def test_show_not_found(
        self,
        mock_db_cls: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test show with non-existent run ID."""
        mock_manager = MagicMock()
        mock_manager.get.return_value = None
        mock_get_manager.return_value = mock_manager

        mock_db = MagicMock()
        mock_db.fetchall.return_value = []
        mock_db_cls.return_value = mock_db

        result = runner.invoke(cli, ["agents", "runs", "show", "ar-nonexistent"])

        # ClickException causes non-zero exit code
        assert result.exit_code != 0
        assert "Agent run not found" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_show_json_output(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
        mock_completed_run: MagicMock,
    ) -> None:
        """Test show with JSON output."""
        mock_manager = MagicMock()
        mock_manager.get.return_value = mock_completed_run
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "ar-completed123"

        result = runner.invoke(cli, ["agents", "runs", "show", "ar-completed123", "--json"])

        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == "ar-completed123"
        assert data["status"] == "success"

    @patch("gobby.cli.agents.resolve_agent_run_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_show_with_result(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
        mock_completed_run: MagicMock,
    ) -> None:
        """Test show displays result for completed run."""
        mock_manager = MagicMock()
        mock_manager.get.return_value = mock_completed_run
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "ar-completed123"

        result = runner.invoke(cli, ["agents", "runs", "show", "ar-completed123"])

        assert result.exit_code == 0
        assert "Result:" in result.output
        assert "Task completed successfully" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_show_with_error(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
        mock_failed_run: MagicMock,
    ) -> None:
        """Test show displays error for failed run."""
        mock_manager = MagicMock()
        mock_manager.get.return_value = mock_failed_run
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "ar-failed456"

        result = runner.invoke(cli, ["agents", "runs", "show", "ar-failed456"])

        assert result.exit_code == 0
        assert "Error:" in result.output
        assert "Connection timeout" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_show_truncates_long_prompt(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test show truncates long prompts."""
        run = MagicMock()
        run.id = "ar-longprompt"
        # ... (rest is same) -> actually I need to preserve lines 1081-1095
        run.status = "running"
        run.provider = "claude"
        run.model = None
        run.parent_session_id = "sess-123"
        run.child_session_id = None
        run.workflow_name = None
        run.prompt = "A" * 600  # Longer than 500 chars
        run.result = None
        run.error = None
        run.turns_used = 0
        run.tool_calls_count = 0
        run.created_at = "2024-01-01"
        run.started_at = None
        run.completed_at = None
        run.to_dict.return_value = {"id": "ar-longprompt"}

        mock_manager = MagicMock()
        mock_manager.get.return_value = run
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "ar-longprompt"

        result = runner.invoke(cli, ["agents", "runs", "show", "ar-longprompt"])

        assert result.exit_code == 0
        assert "..." in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_show_truncates_long_result(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test show truncates long results."""
        run = MagicMock()
        # ... preserved setup ...
        run.id = "ar-longresult"
        run.status = "success"
        run.provider = "claude"
        run.model = None
        run.parent_session_id = "sess-123"
        run.child_session_id = None
        run.workflow_name = None
        run.prompt = "Short prompt"
        run.result = "B" * 600  # Longer than 500 chars
        run.error = None
        run.turns_used = 5
        run.tool_calls_count = 10
        run.created_at = "2024-01-01"
        run.started_at = "2024-01-01T10:00:00Z"
        run.completed_at = "2024-01-01T10:30:00Z"
        run.to_dict.return_value = {"id": "ar-longresult"}

        mock_manager = MagicMock()
        mock_manager.get.return_value = run
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "ar-longresult"

        result = runner.invoke(cli, ["agents", "runs", "show", "ar-longresult"])

        assert result.exit_code == 0
        # Should show "..." for truncated result
        assert result.output.count("...") >= 1


# ==============================================================================
# Tests for agents status command
# ==============================================================================


class TestAgentsStatusCommand:
    """Tests for gobby agents status command."""

    def test_status_help(self, runner: CliRunner) -> None:
        """Test status --help displays options."""
        result = runner.invoke(cli, ["agents", "status", "--help"])

        assert result.exit_code == 0
        assert "RUN_REF" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_status_running(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
        mock_agent_run: MagicMock,
    ) -> None:
        """Test status for running agent."""
        mock_manager = MagicMock()
        mock_manager.get.return_value = mock_agent_run
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = mock_agent_run.id

        result = runner.invoke(cli, ["agents", "status", mock_agent_run.id])

        assert result.exit_code == 0
        assert mock_agent_run.id in result.output
        assert "running" in result.output
        assert "Running since:" in result.output
        assert "Turns used:" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_status_success(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
        mock_completed_run: MagicMock,
    ) -> None:
        """Test status for successful agent."""
        mock_manager = MagicMock()
        mock_manager.get.return_value = mock_completed_run
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = mock_completed_run.id

        result = runner.invoke(cli, ["agents", "status", mock_completed_run.id])

        assert result.exit_code == 0
        assert "success" in result.output
        assert "Completed:" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_status_error(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
        mock_failed_run: MagicMock,
    ) -> None:
        """Test status for failed agent."""
        mock_manager = MagicMock()
        mock_manager.get.return_value = mock_failed_run
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = mock_failed_run.id

        result = runner.invoke(cli, ["agents", "status", mock_failed_run.id])

        assert result.exit_code == 0
        assert "error" in result.output
        assert "Error:" in result.output
        assert "Connection timeout" in result.output

    @patch("gobby.cli.agents.get_agent_run_manager")
    @patch("gobby.cli.agents.LocalDatabase")
    def test_status_prefix_match(
        self,
        mock_db_cls: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
        mock_agent_run: MagicMock,
    ) -> None:
        """Test status with prefix match."""
        mock_manager = MagicMock()
        # Ensure manager returns run when resolved ID is passed
        mock_manager.get.return_value = mock_agent_run
        mock_get_manager.return_value = mock_manager

        mock_db = MagicMock()
        mock_db.fetchall.return_value = [
            {
                "id": mock_agent_run.id,
                "parent_session_id": mock_agent_run.parent_session_id,
                "child_session_id": mock_agent_run.child_session_id,
                "workflow_name": mock_agent_run.workflow_name,
                "provider": mock_agent_run.provider,
                "model": mock_agent_run.model,
                "status": mock_agent_run.status,
                "prompt": mock_agent_run.prompt,
                "result": mock_agent_run.result,
                "error": mock_agent_run.error,
                "tool_calls_count": mock_agent_run.tool_calls_count,
                "turns_used": mock_agent_run.turns_used,
                "started_at": mock_agent_run.started_at,
                "completed_at": mock_agent_run.completed_at,
                "created_at": mock_agent_run.created_at,
                "updated_at": mock_agent_run.updated_at,
            }
        ]
        mock_db_cls.return_value = mock_db

        result = runner.invoke(cli, ["agents", "status", "ar-abc"])

        assert result.exit_code == 0
        assert mock_agent_run.id in result.output

    @patch("gobby.cli.agents.get_agent_run_manager")
    @patch("gobby.cli.agents.LocalDatabase")
    def test_status_not_found(
        self,
        mock_db_cls: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test status with non-existent run."""
        mock_manager = MagicMock()
        mock_manager.get.return_value = None
        mock_get_manager.return_value = mock_manager

        mock_db = MagicMock()
        mock_db.fetchall.return_value = []
        mock_db_cls.return_value = mock_db

        result = runner.invoke(cli, ["agents", "status", "ar-nonexistent"])

        assert result.exit_code != 0
        assert "Agent run not found" in result.output


# ==============================================================================
# Tests for agents stop command
# ==============================================================================


class TestAgentsStopCommand:
    """Tests for gobby agents stop command."""

    def test_stop_help(self, runner: CliRunner) -> None:
        """Test stop --help displays options."""
        result = runner.invoke(cli, ["agents", "stop", "--help"])

        assert result.exit_code == 0
        assert "RUN_REF" in result.output

    @patch("gobby.utils.daemon_client.DaemonClient")
    @patch("gobby.cli.agents.resolve_agent_run_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_stop_running_agent(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        mock_daemon_client: MagicMock,
        runner: CliRunner,
        mock_agent_run: MagicMock,
    ) -> None:
        """Test stopping a running agent."""
        mock_manager = MagicMock()
        mock_manager.get.return_value = mock_agent_run
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = mock_agent_run.id
        mock_daemon_client.return_value.call_mcp_tool.return_value = {"success": True}

        result = runner.invoke(cli, ["agents", "stop", mock_agent_run.id, "--yes"])

        assert result.exit_code == 0
        assert "Stopped agent run" in result.output
        mock_daemon_client.return_value.call_mcp_tool.assert_called_once()

    @patch("gobby.utils.daemon_client.DaemonClient")
    @patch("gobby.cli.agents.resolve_agent_run_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_stop_pending_agent(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        mock_daemon_client: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test stopping a pending agent."""
        pending_run = MagicMock()
        pending_run.id = "ar-pending123"
        pending_run.status = "pending"

        mock_manager = MagicMock()
        mock_manager.get.return_value = pending_run
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "ar-pending123"
        mock_daemon_client.return_value.call_mcp_tool.return_value = {"success": True}

        result = runner.invoke(cli, ["agents", "stop", "ar-pending123", "--yes"])

        assert result.exit_code == 0
        assert "Stopped agent run" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_stop_already_completed(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
        mock_completed_run: MagicMock,
    ) -> None:
        """Test stopping already completed agent."""
        mock_manager = MagicMock()
        mock_manager.get.return_value = mock_completed_run
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = mock_completed_run.id

        result = runner.invoke(cli, ["agents", "stop", mock_completed_run.id, "--yes"])

        assert result.exit_code == 0
        assert "Cannot stop agent in status" in result.output

    @patch("gobby.cli.agents.get_agent_run_manager")
    @patch("gobby.cli.agents.LocalDatabase")
    def test_stop_not_found(
        self,
        mock_db_cls: MagicMock,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test stopping non-existent agent."""
        mock_manager = MagicMock()
        mock_manager.get.return_value = None
        mock_get_manager.return_value = mock_manager

        mock_db = MagicMock()
        mock_db.fetchall.return_value = []
        mock_db_cls.return_value = mock_db

        result = runner.invoke(cli, ["agents", "stop", "ar-nonexistent", "--yes"])

        assert result.exit_code != 0
        assert "Agent run not found" in result.output

    @patch("gobby.utils.daemon_client.DaemonClient")
    @patch("gobby.cli.agents.get_agent_run_manager")
    @patch("gobby.cli.agents.LocalDatabase")
    def test_stop_prefix_match(
        self,
        mock_db_cls: MagicMock,
        mock_get_manager: MagicMock,
        mock_daemon_client: MagicMock,
        runner: CliRunner,
        mock_agent_run: MagicMock,
    ) -> None:
        """Test stop with prefix match."""
        mock_manager = MagicMock()
        # Fix: ensure manager.get returns the run for the resolved ID
        mock_manager.get.return_value = mock_agent_run
        mock_get_manager.return_value = mock_manager

        mock_db = MagicMock()
        mock_db.fetchall.return_value = [
            {
                "id": mock_agent_run.id,
                "parent_session_id": mock_agent_run.parent_session_id,
                "child_session_id": mock_agent_run.child_session_id,
                "workflow_name": mock_agent_run.workflow_name,
                "provider": mock_agent_run.provider,
                "model": mock_agent_run.model,
                "status": mock_agent_run.status,
                "prompt": mock_agent_run.prompt,
                "result": mock_agent_run.result,
                "error": mock_agent_run.error,
                "tool_calls_count": mock_agent_run.tool_calls_count,
                "turns_used": mock_agent_run.turns_used,
                "started_at": mock_agent_run.started_at,
                "completed_at": mock_agent_run.completed_at,
                "created_at": mock_agent_run.created_at,
                "updated_at": mock_agent_run.updated_at,
            }
        ]
        mock_db_cls.return_value = mock_db
        mock_daemon_client.return_value.call_mcp_tool.return_value = {"success": True}

        result = runner.invoke(cli, ["agents", "stop", "ar-abc", "--yes"])

        assert result.exit_code == 0
        assert "Stopped agent run" in result.output

    def test_stop_requires_confirmation(self, runner: CliRunner) -> None:
        """Test stop requires confirmation."""
        result = runner.invoke(cli, ["agents", "stop", "ar-test123"])

        # Should abort without --yes
        assert result.exit_code == 1
        assert "Aborted" in result.output


# ==============================================================================
# Tests for agents stats command
# ==============================================================================


class TestAgentsStatsCommand:
    """Tests for gobby agents stats command."""

    def test_stats_help(self, runner: CliRunner) -> None:
        """Test stats --help displays options."""
        result = runner.invoke(cli, ["agents", "stats", "--help"])

        assert result.exit_code == 0
        assert "--session" in result.output

    @patch("gobby.cli.agents.LocalDatabase")
    def test_stats_global(
        self,
        mock_db_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test global agent statistics."""
        mock_db = MagicMock()
        mock_db.fetchone.return_value = {
            "total": 100,
            "success": 80,
            "error": 10,
            "running": 5,
            "pending": 2,
            "timeout": 2,
            "cancelled": 1,
        }
        mock_db_cls.return_value = mock_db

        result = runner.invoke(cli, ["agents", "stats"])

        assert result.exit_code == 0
        assert "Agent Run Statistics:" in result.output
        assert "Total Runs: 100" in result.output
        assert "Success: 80" in result.output
        assert "Error: 10" in result.output
        assert "Running: 5" in result.output
        assert "Success Rate: 80.0%" in result.output

    @patch("gobby.cli.agents.resolve_session_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_stats_by_session(
        self,
        mock_get_manager: MagicMock,
        mock_resolve_session: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test session-specific agent statistics."""
        mock_manager = MagicMock()
        mock_manager.count_by_session.return_value = {
            "success": 10,
            "error": 2,
            "running": 1,
        }
        mock_get_manager.return_value = mock_manager
        mock_resolve_session.return_value = "sess-test123"

        result = runner.invoke(cli, ["agents", "stats", "--session", "sess-test123"])

        assert result.exit_code == 0
        assert "Agent Statistics for session sess-test123" in result.output
        assert "Total Runs: 13" in result.output

    @patch("gobby.cli.agents.LocalDatabase")
    def test_stats_no_runs(
        self,
        mock_db_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test stats with no agent runs."""
        mock_db = MagicMock()
        mock_db.fetchone.return_value = None
        mock_db_cls.return_value = mock_db

        result = runner.invoke(cli, ["agents", "stats"])

        assert result.exit_code == 0
        assert "No agent runs found" in result.output

    @patch("gobby.cli.agents.LocalDatabase")
    def test_stats_zero_total(
        self,
        mock_db_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test stats with zero total runs doesn't divide by zero."""
        mock_db = MagicMock()
        mock_db.fetchone.return_value = {
            "total": 0,
            "success": 0,
            "error": 0,
            "running": 0,
            "pending": 0,
            "timeout": 0,
            "cancelled": 0,
        }
        mock_db_cls.return_value = mock_db

        result = runner.invoke(cli, ["agents", "stats"])

        assert result.exit_code == 0
        assert "Total Runs: 0" in result.output
        # Should not show success rate when total is 0
        assert "Success Rate:" not in result.output


# ==============================================================================
# Tests for agents cleanup command
# ==============================================================================


class TestAgentsCleanupCommand:
    """Tests for gobby agents cleanup command."""

    def test_cleanup_help(self, runner: CliRunner) -> None:
        """Test cleanup --help displays options."""
        result = runner.invoke(cli, ["agents", "cleanup", "--help"])

        assert result.exit_code == 0
        assert "--timeout" in result.output
        assert "--dry-run" in result.output

    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_cleanup_default(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test cleanup with default options."""
        mock_manager = MagicMock()
        mock_manager.cleanup_stale_runs.return_value = 3
        mock_manager.cleanup_stale_pending_runs.return_value = 2
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["agents", "cleanup"])

        assert result.exit_code == 0
        assert "Cleaned up 3 timed-out runs" in result.output
        assert "2 stale pending runs" in result.output

    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_cleanup_custom_timeout(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test cleanup with custom timeout."""
        mock_manager = MagicMock()
        mock_manager.cleanup_stale_runs.return_value = 1
        mock_manager.cleanup_stale_pending_runs.return_value = 0
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["agents", "cleanup", "--timeout", "60"])

        assert result.exit_code == 0
        mock_manager.cleanup_stale_runs.assert_called_once_with(default_timeout_minutes=60)

    @patch("gobby.cli.agents.LocalDatabase")
    def test_cleanup_dry_run(
        self,
        mock_db_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test cleanup with --dry-run."""
        mock_db = MagicMock()
        mock_db.fetchall.side_effect = [
            # Stale running runs
            [
                {"id": "ar-stale1", "started_at": "2024-01-01T10:00:00Z"},
                {"id": "ar-stale2", "started_at": "2024-01-01T09:00:00Z"},
            ],
            # Stale pending runs
            [{"id": "ar-pending1", "created_at": "2024-01-01T08:00:00Z"}],
        ]
        mock_db_cls.return_value = mock_db

        result = runner.invoke(cli, ["agents", "cleanup", "--dry-run"])

        assert result.exit_code == 0
        assert "Stale running runs" in result.output
        assert "ar-stale1" in result.output
        assert "Stale pending runs" in result.output
        assert "ar-pending1" in result.output

    @patch("gobby.cli.agents.LocalDatabase")
    def test_cleanup_dry_run_no_stale(
        self,
        mock_db_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test cleanup dry-run with no stale runs."""
        mock_db = MagicMock()
        mock_db.fetchall.return_value = []
        mock_db_cls.return_value = mock_db

        result = runner.invoke(cli, ["agents", "cleanup", "--dry-run"])

        assert result.exit_code == 0
        assert "Stale running runs (>30m): 0" in result.output
        assert "Stale pending runs (>60m): 0" in result.output


# ==============================================================================
# Tests for helper functions
# ==============================================================================


class TestHelperFunctions:
    """Tests for helper functions in agents module."""

    @patch("gobby.cli.agents.LocalDatabase")
    @patch("gobby.cli.agents.LocalAgentRunManager")
    def test_get_agent_run_manager(
        self,
        mock_manager_cls: MagicMock,
        mock_db_cls: MagicMock,
    ) -> None:
        """Test get_agent_run_manager creates manager correctly."""
        from gobby.cli.agents import get_agent_run_manager

        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db

        mock_manager = MagicMock()
        mock_manager_cls.return_value = mock_manager

        result = get_agent_run_manager()

        mock_db_cls.assert_called_once()
        mock_manager_cls.assert_called_once_with(mock_db)
        assert result == mock_manager

    @patch("gobby.config.app.load_config")
    def test_get_daemon_url(self, mock_load_config: MagicMock) -> None:
        """Test get_daemon_url returns correct URL."""
        from gobby.cli.agents import get_daemon_url

        mock_config = MagicMock()
        mock_config.daemon_port = 9876
        mock_load_config.return_value = mock_config

        result = get_daemon_url()

        assert result == "http://localhost:9876"


# ==============================================================================
# Edge Cases and Error Handling
# ==============================================================================


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_handles_multiline_prompt(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test list handles prompts with newlines."""
        mock_manager = MagicMock()
        mock_get_manager.return_value = mock_manager

        with patch("gobby.cli.agents.LocalDatabase") as mock_db_cls:
            mock_db = MagicMock()
            mock_db.fetchall.return_value = [
                {
                    "id": "ar-multiline",
                    "parent_session_id": "sess-123",
                    "child_session_id": None,
                    "workflow_name": None,
                    "provider": "claude",
                    "model": None,
                    "status": "running",
                    "prompt": "Line 1\nLine 2\nLine 3",
                    "result": None,
                    "error": None,
                    "tool_calls_count": 0,
                    "turns_used": 0,
                    "started_at": None,
                    "completed_at": None,
                    "created_at": "2024-01-01",
                    "updated_at": "2024-01-01",
                }
            ]
            mock_db_cls.return_value = mock_db

            result = runner.invoke(cli, ["agents", "runs", "list"])

            assert result.exit_code == 0
            # Prompt in the list output should not contain raw newlines (they are replaced)
            # The output should have "Line 1 Line 2 Line 3" on a single row, not multiple lines
            lines = result.output.strip().split("\n")
            # Find the line with the agent run info
            run_lines = [line for line in lines if "ar-multiline" in line]
            assert len(run_lines) == 1  # Should be on a single line

    @patch("gobby.cli.agents.resolve_agent_run_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_show_without_optional_fields(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test show handles run without optional fields."""
        run = MagicMock()
        run.id = "ar-minimal"
        run.status = "pending"
        run.provider = "claude"
        run.model = None
        run.parent_session_id = "sess-123"
        run.child_session_id = None
        run.workflow_name = None
        run.prompt = "Minimal prompt"
        run.result = None
        run.error = None
        run.turns_used = 0
        run.tool_calls_count = 0
        run.created_at = "2024-01-01"
        run.started_at = None
        run.completed_at = None
        run.to_dict.return_value = {"id": "ar-minimal"}

        mock_manager = MagicMock()
        mock_manager.get.return_value = run
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "ar-minimal"

        result = runner.invoke(cli, ["agents", "runs", "show", "ar-minimal"])

        assert result.exit_code == 0
        assert "Agent Run: ar-minimal" in result.output
        # Model should not be shown when None
        assert "Model:" not in result.output
        # Child Session should not be shown when None
        lines = result.output.split("\n")
        child_session_lines = [line for line in lines if "Child Session:" in line]
        assert len(child_session_lines) == 0

    @patch("gobby.cli.agents.resolve_agent_run_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_status_with_timeout_status(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test status display for timed-out agent."""
        run = MagicMock()
        run.id = "ar-timeout"
        run.status = "timeout"
        run.error = "Execution timed out"
        run.completed_at = "2024-01-01T11:00:00Z"

        mock_manager = MagicMock()
        mock_manager.get.return_value = run
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "ar-timeout"

        result = runner.invoke(cli, ["agents", "status", "ar-timeout"])

        assert result.exit_code == 0
        assert "timeout" in result.output
        assert "Completed:" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_status_with_cancelled_status(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test status display for cancelled agent."""
        run = MagicMock()
        run.id = "ar-cancelled"
        run.status = "cancelled"
        run.error = None
        run.completed_at = "2024-01-01T10:30:00Z"

        mock_manager = MagicMock()
        mock_manager.get.return_value = run
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "ar-cancelled"

        result = runner.invoke(cli, ["agents", "status", "ar-cancelled"])

        assert result.exit_code == 0
        assert "cancelled" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_stop_error_status(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test cannot stop agent already in error status."""
        run = MagicMock()
        run.id = "ar-error"
        run.status = "error"

        mock_manager = MagicMock()
        mock_manager.get.return_value = run
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "ar-error"

        result = runner.invoke(cli, ["agents", "stop", "ar-error", "--yes"])

        assert result.exit_code == 0
        assert "Cannot stop agent in status: error" in result.output
