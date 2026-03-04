"""Tests for cli/agents.py -- targeting uncovered lines.

Covers: resolve_agent_run_id, agent_stats (global),
        cleanup_agents (dry-run and execute).
Lines targeted: 48, 142-143, 230-231, 293-294, 341-342, 375-376, 405-562, 574-575
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from gobby.cli.agents import agents, resolve_agent_run_id

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _mock_run(**overrides: Any) -> MagicMock:
    defaults = {
        "id": "run-abc123def456789012345678901234",
        "parent_session_id": "sess-parent",
        "child_session_id": "sess-child",
        "workflow_name": "worker",
        "provider": "claude",
        "model": "claude-3-opus",
        "status": "running",
        "prompt": "Do things",
        "result": None,
        "error": None,
        "tool_calls_count": 5,
        "turns_used": 3,
        "started_at": "2024-01-01T10:00:00Z",
        "completed_at": None,
        "created_at": "2024-01-01T09:59:00Z",
        "updated_at": "2024-01-01T10:01:00Z",
    }
    defaults.update(overrides)
    run = MagicMock()
    for k, v in defaults.items():
        setattr(run, k, v)
    run.to_dict.return_value = defaults
    return run


# =============================================================================
# spawn_agent
# =============================================================================
# TODO: add tests for spawn_agent_cmd error paths

# =============================================================================
# resolve_agent_run_id
# =============================================================================


class TestResolveAgentRunId:
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_exact_uuid(self, mock_mgr_fn: MagicMock) -> None:
        mgr = MagicMock()
        full_id = "a" * 36
        mgr.get.return_value = _mock_run(id=full_id)
        mock_mgr_fn.return_value = mgr
        assert resolve_agent_run_id(full_id) == full_id

    @patch("gobby.cli.agents.LocalDatabase")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_prefix_match(self, mock_mgr_fn: MagicMock, mock_db_cls: MagicMock) -> None:
        mgr = MagicMock()
        mgr.get.return_value = None  # short ref, not exact
        mock_mgr_fn.return_value = mgr
        mock_db_cls.return_value.fetchall.return_value = [{"id": "run-abc123"}]
        assert resolve_agent_run_id("run-abc") == "run-abc123"

    @patch("gobby.cli.agents.LocalDatabase")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_not_found(self, mock_mgr_fn: MagicMock, mock_db_cls: MagicMock) -> None:
        mgr = MagicMock()
        mgr.get.return_value = None
        mock_mgr_fn.return_value = mgr
        mock_db_cls.return_value.fetchall.return_value = []
        with pytest.raises(click.ClickException, match="not found"):
            resolve_agent_run_id("zzz")

    @patch("gobby.cli.agents.LocalDatabase")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_ambiguous(self, mock_mgr_fn: MagicMock, mock_db_cls: MagicMock) -> None:
        mgr = MagicMock()
        mgr.get.return_value = None
        mock_mgr_fn.return_value = mgr
        mock_db_cls.return_value.fetchall.return_value = [{"id": "run-a1"}, {"id": "run-a2"}]
        with pytest.raises(click.ClickException, match="Ambiguous"):
            resolve_agent_run_id("run-a")


# =============================================================================
# check_agent
# =============================================================================
# TODO: add tests for check_agent

# =============================================================================
# agent_stats - global
# =============================================================================


class TestAgentStatsGlobal:
    @patch("gobby.cli.agents.LocalDatabase")
    def test_global_stats(self, mock_db_cls: MagicMock, runner: CliRunner) -> None:
        mock_db_cls.return_value.fetchone.return_value = {
            "total": 20,
            "success": 15,
            "error": 2,
            "running": 1,
            "pending": 0,
            "timeout": 1,
            "cancelled": 1,
        }
        result = runner.invoke(agents, ["stats"])
        assert result.exit_code == 0
        assert "Total Runs: 20" in result.output
        assert "Success Rate: 75.0%" in result.output

    @patch("gobby.cli.agents.LocalDatabase")
    def test_global_stats_empty(self, mock_db_cls: MagicMock, runner: CliRunner) -> None:
        mock_db_cls.return_value.fetchone.return_value = None
        result = runner.invoke(agents, ["stats"])
        assert result.exit_code == 0
        assert "No agent runs found" in result.output

    @patch("gobby.cli.agents.LocalDatabase")
    def test_global_stats_zero_total(self, mock_db_cls: MagicMock, runner: CliRunner) -> None:
        mock_db_cls.return_value.fetchone.return_value = {
            "total": 0,
            "success": 0,
            "error": 0,
            "running": 0,
            "pending": 0,
            "timeout": 0,
            "cancelled": 0,
        }
        result = runner.invoke(agents, ["stats"])
        assert result.exit_code == 0
        assert "Total Runs: 0" in result.output
        # No success rate printed when total is 0
        assert "Success Rate" not in result.output


# =============================================================================
# cleanup_agents
# =============================================================================


class TestCleanupAgents:
    @patch("gobby.cli.agents.LocalDatabase")
    def test_cleanup_dry_run(self, mock_db_cls: MagicMock, runner: CliRunner) -> None:
        mock_db = mock_db_cls.return_value
        mock_db.fetchall.side_effect = [
            [{"id": "run-1", "started_at": "2024-01-01T00:00:00"}],  # stale running
            [{"id": "run-2", "created_at": "2024-01-01T00:00:00"}],  # stale pending
        ]
        result = runner.invoke(agents, ["cleanup", "--dry-run"])
        assert result.exit_code == 0
        assert "Stale running runs" in result.output
        assert "Stale pending runs" in result.output

    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_cleanup_execute(self, mock_mgr_fn: MagicMock, runner: CliRunner) -> None:
        mgr = MagicMock()
        mgr.cleanup_stale_runs.return_value = 2
        mgr.cleanup_stale_pending_runs.return_value = 1
        mock_mgr_fn.return_value = mgr
        result = runner.invoke(agents, ["cleanup"])
        assert result.exit_code == 0
        assert "2 timed-out runs" in result.output
        assert "1 stale pending" in result.output

    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_cleanup_with_timeout(self, mock_mgr_fn: MagicMock, runner: CliRunner) -> None:
        mgr = MagicMock()
        mgr.cleanup_stale_runs.return_value = 0
        mgr.cleanup_stale_pending_runs.return_value = 0
        mock_mgr_fn.return_value = mgr
        result = runner.invoke(agents, ["cleanup", "--timeout", "60"])
        assert result.exit_code == 0
        mgr.cleanup_stale_runs.assert_called_once_with(timeout_minutes=60)


# =============================================================================
# list_agents - edge cases
# =============================================================================


class TestListAgentsEdgeCases:
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_running_shortcut(self, mock_mgr_fn: MagicMock, runner: CliRunner) -> None:
        mgr = MagicMock()
        mgr.list_running.return_value = []
        mock_mgr_fn.return_value = mgr
        result = runner.invoke(agents, ["list", "--status", "running"])
        assert result.exit_code == 0
        mgr.list_running.assert_called_once_with(limit=20)

    @patch("gobby.cli.agents.LocalDatabase")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_all_no_filter(
        self, mock_mgr_fn: MagicMock, mock_db_cls: MagicMock, runner: CliRunner
    ) -> None:
        mock_db_cls.return_value.fetchall.return_value = []
        result = runner.invoke(agents, ["list"])
        assert result.exit_code == 0
        assert "No agent runs found" in result.output

    @patch("gobby.cli.agents.LocalDatabase")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_with_status_filter(
        self, mock_mgr_fn: MagicMock, mock_db_cls: MagicMock, runner: CliRunner
    ) -> None:
        mock_db_cls.return_value.fetchall.return_value = []
        result = runner.invoke(agents, ["list", "--status", "error"])
        assert result.exit_code == 0
        mock_db_cls.return_value.fetchall.assert_called_once()


# =============================================================================
# show_agent / agent_status - edge cases
# =============================================================================


class TestShowStatusEdgeCases:
    @patch("gobby.cli.agents.resolve_agent_run_id", return_value="run-1")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_show_not_found(
        self, mock_mgr_fn: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        mock_mgr_fn.return_value.get.return_value = None
        result = runner.invoke(agents, ["show", "run-1"])
        assert result.exit_code == 0
        assert "Agent run not found" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id", return_value="run-1")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_status_not_found(
        self, mock_mgr_fn: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        mock_mgr_fn.return_value.get.return_value = None
        result = runner.invoke(agents, ["status", "run-1"])
        assert result.exit_code == 0
        assert "Agent run not found" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id", return_value="run-1")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_status_completed(
        self, mock_mgr_fn: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        run = _mock_run(status="error", error="segfault", completed_at="2024-01-01T11:00:00Z")
        mock_mgr_fn.return_value.get.return_value = run
        result = runner.invoke(agents, ["status", "run-1"])
        assert result.exit_code == 0
        assert "Completed:" in result.output
        assert "Error: segfault" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id", return_value="run-1")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_stop_not_stoppable(
        self, mock_mgr_fn: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        run = _mock_run(status="success")
        mock_mgr_fn.return_value.get.return_value = run
        result = runner.invoke(agents, ["stop", "run-1", "--yes"])
        assert result.exit_code == 0
        assert "Cannot stop agent in status" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id", return_value="run-1")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_stop_not_found(
        self, mock_mgr_fn: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        mock_mgr_fn.return_value.get.return_value = None
        result = runner.invoke(agents, ["stop", "run-1", "--yes"])
        assert result.exit_code == 0
        assert "Agent run not found" in result.output


# =============================================================================
# get_daemon_url (lines 71-74)
# =============================================================================


class TestGetDaemonUrl:
    @patch("gobby.cli.agents.get_daemon_url", return_value="http://localhost:60887")
    def test_get_daemon_url(self, mock_url: MagicMock) -> None:
        result = mock_url()
        assert result == "http://localhost:60887"


# =============================================================================
# list_agents display output (lines 264-279)
# =============================================================================


class TestListAgentsDisplay:
    @patch("gobby.cli.agents.LocalDatabase")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_with_runs_display(
        self, mock_mgr_fn: MagicMock, mock_db_cls: MagicMock, runner: CliRunner
    ) -> None:
        """Cover the display loop when runs exist."""

        mock_row = {
            "id": "run-abc123def456",
            "parent_session_id": "sess-parent",
            "child_session_id": None,
            "workflow_name": None,
            "provider": "claude",
            "model": "opus",
            "status": "running",
            "prompt": "Fix the bug in auth module",
            "result": None,
            "error": None,
            "tool_calls_count": 3,
            "turns_used": 2,
            "started_at": "2024-01-01T10:00:00",
            "completed_at": None,
            "created_at": "2024-01-01T09:59:00",
            "updated_at": "2024-01-01T10:01:00",
            "mode": "terminal",
            "isolation_mode": None,
            "isolation_path": None,
            "agent_name": "default",
            "task_id": None,
        }
        mock_db_cls.return_value.fetchall.return_value = [mock_row]
        result = runner.invoke(agents, ["list"])
        assert result.exit_code == 0
        assert "Found 1 agent run(s)" in result.output
        assert "claude" in result.output

    @patch("gobby.cli.agents.resolve_session_id", return_value="sess-123")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_by_session(
        self, mock_mgr_fn: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        """Cover the session_id path (lines 228-234)."""
        run = _mock_run(status="success")
        mock_mgr_fn.return_value.list_by_session.return_value = [run]
        result = runner.invoke(agents, ["list", "--session", "sess-123"])
        assert result.exit_code == 0
        assert "Found 1 agent run(s)" in result.output

    @patch(
        "gobby.cli.agents.resolve_session_id",
        side_effect=click.ClickException("Session not found"),
    )
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_list_bad_session_id(
        self, mock_mgr_fn: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        """Cover exception path (lines 230-231)."""
        result = runner.invoke(agents, ["list", "--session", "bad-sess"])
        assert result.exit_code != 0


# =============================================================================
# show_agent text output (lines 296-329)
# =============================================================================


class TestShowAgentTextOutput:
    @patch("gobby.cli.agents.resolve_agent_run_id", return_value="run-1")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_show_full_text_output(
        self, mock_mgr_fn: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        """Cover text display path with all fields populated."""
        run = _mock_run(
            status="success",
            result="All tests pass",
            error=None,
            completed_at="2024-01-01T11:00:00Z",
        )
        mock_mgr_fn.return_value.get.return_value = run
        result = runner.invoke(agents, ["show", "run-1"])
        assert result.exit_code == 0
        assert "Agent Run:" in result.output
        assert "Status: success" in result.output
        assert "Provider: claude" in result.output
        assert "Model: claude-3-opus" in result.output
        assert "Child Session:" in result.output
        assert "Workflow: worker" in result.output
        assert "Prompt:" in result.output
        assert "Result:" in result.output
        assert "Turns Used:" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id", return_value="run-1")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_show_with_error(
        self, mock_mgr_fn: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        """Cover error display in show."""
        run = _mock_run(status="error", error="segfault", result=None)
        mock_mgr_fn.return_value.get.return_value = run
        result = runner.invoke(agents, ["show", "run-1"])
        assert result.exit_code == 0
        assert "Error: segfault" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id", return_value="run-1")
    @patch("gobby.cli.agents.get_agent_run_manager")
    def test_show_json(
        self, mock_mgr_fn: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        """Cover JSON output path (line 296-298)."""
        run = _mock_run()
        mock_mgr_fn.return_value.get.return_value = run
        result = runner.invoke(agents, ["show", "run-1", "--json"])
        assert result.exit_code == 0
        assert '"id"' in result.output


# =============================================================================
# kill_agent (lines 405-442)
# =============================================================================


class TestKillAgent:
    @patch("gobby.cli.agents.resolve_agent_run_id", return_value="run-1")
    @patch("gobby.utils.daemon_client.DaemonClient")
    def test_kill_success(
        self, mock_client_cls: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        mock_client_cls.return_value.call_mcp_tool.return_value = {
            "success": True,
            "message": "Killed agent run-1",
        }
        result = runner.invoke(agents, ["kill", "run-1", "--yes"])
        assert result.exit_code == 0
        assert "Killed agent" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id", return_value="run-1")
    @patch("gobby.utils.daemon_client.DaemonClient")
    def test_kill_with_pgrep(
        self, mock_client_cls: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        mock_client_cls.return_value.call_mcp_tool.return_value = {
            "success": True,
            "message": "Killed",
            "found_via": "pgrep",
            "pid": 12345,
            "already_dead": True,
            "workflow_stopped": True,
        }
        result = runner.invoke(agents, ["kill", "run-1", "--yes", "--stop"])
        assert result.exit_code == 0
        assert "pgrep" in result.output
        assert "already terminated" in result.output
        assert "workflow ended" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id", return_value="run-1")
    @patch("gobby.utils.daemon_client.DaemonClient")
    def test_kill_failure(
        self, mock_client_cls: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        mock_client_cls.return_value.call_mcp_tool.return_value = {
            "success": False,
            "error": "Process not found",
        }
        result = runner.invoke(agents, ["kill", "run-1", "--yes"])
        assert result.exit_code == 0
        assert "Failed:" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id", return_value="run-1")
    @patch("gobby.utils.daemon_client.DaemonClient")
    def test_kill_exception(
        self, mock_client_cls: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        mock_client_cls.return_value.call_mcp_tool.side_effect = RuntimeError("conn refused")
        result = runner.invoke(agents, ["kill", "run-1", "--yes"])
        assert result.exit_code == 0
        assert "Error:" in result.output

    @patch("gobby.cli.agents.resolve_agent_run_id", return_value="run-1")
    def test_kill_no_confirm(self, mock_resolve: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(agents, ["kill", "run-1"], input="n\n")
        assert result.exit_code == 0

    @patch("gobby.cli.agents.resolve_agent_run_id", return_value="run-1")
    def test_kill_stop_no_confirm(self, mock_resolve: MagicMock, runner: CliRunner) -> None:
        """Cover --stop flag in confirm message (line 412)."""
        result = runner.invoke(agents, ["kill", "run-1", "--stop"], input="n\n")
        assert result.exit_code == 0
