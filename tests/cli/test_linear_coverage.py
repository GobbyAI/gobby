"""Tests for cli/linear.py — targeting uncovered lines."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.linear import linear

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _mock_linear_deps(
    project_id: str = "proj-123",
    linear_team_id: str | None = None,
    _github_repo: str | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock, str]:
    """Build a mock tuple for get_linear_deps."""
    db = MagicMock()
    task_manager = MagicMock()
    task_manager.db = db
    project_manager = MagicMock()
    project = MagicMock()
    project.linear_team_id = linear_team_id
    project_manager.get.return_value = project
    mcp_manager = MagicMock()
    return task_manager, mcp_manager, project_manager, project_id


# ---------------------------------------------------------------------------
# linear status
# ---------------------------------------------------------------------------
class TestLinearStatus:
    @patch("gobby.cli.linear.LinearIntegration")
    @patch("gobby.cli.linear.get_linear_deps")
    def test_status_text(
        self, mock_deps: MagicMock, mock_integration: MagicMock, runner: CliRunner
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps(linear_team_id="TEAM-1")
        mock_deps.return_value = (tm, mcp, pm, pid)
        tm.db.fetchone.return_value = {"count": 5}

        mock_li = mock_integration.return_value
        mock_li.is_available.return_value = True

        result = runner.invoke(linear, ["status"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "TEAM-1" in result.output
        assert "5" in result.output

    @patch("gobby.cli.linear.LinearIntegration")
    @patch("gobby.cli.linear.get_linear_deps")
    def test_status_json(
        self, mock_deps: MagicMock, mock_integration: MagicMock, runner: CliRunner
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        tm.db.fetchone.return_value = {"count": 0}

        mock_li = mock_integration.return_value
        mock_li.is_available.return_value = False
        mock_li.get_unavailable_reason.return_value = "No API key"

        result = runner.invoke(linear, ["status", "--json"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No API key" in result.output

    @patch("gobby.cli.linear.get_linear_deps", side_effect=Exception("boom"))
    def test_status_exception(self, _deps: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(linear, ["status"], catch_exceptions=False)
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# linear link / unlink
# ---------------------------------------------------------------------------
class TestLinearLink:
    @patch("gobby.cli.linear.get_linear_deps")
    def test_link(self, mock_deps: MagicMock, runner: CliRunner) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        result = runner.invoke(linear, ["link", "TEAM-42"], catch_exceptions=False)
        assert result.exit_code == 0
        pm.update.assert_called_once_with(pid, linear_team_id="TEAM-42")

    @patch("gobby.cli.linear.get_linear_deps", side_effect=Exception("boom"))
    def test_link_error(self, _deps: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(linear, ["link", "TEAM-1"], catch_exceptions=False)
        assert result.exit_code != 0


class TestLinearUnlink:
    @patch("gobby.cli.linear.get_linear_deps")
    def test_unlink(self, mock_deps: MagicMock, runner: CliRunner) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        result = runner.invoke(linear, ["unlink"], catch_exceptions=False)
        assert result.exit_code == 0
        pm.update.assert_called_once_with(pid, linear_team_id=None)

    @patch("gobby.cli.linear.get_linear_deps", side_effect=Exception("boom"))
    def test_unlink_error(self, _deps: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(linear, ["unlink"], catch_exceptions=False)
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# linear import
# ---------------------------------------------------------------------------
class TestLinearImport:
    @patch("gobby.cli.linear.asyncio.run")
    @patch("gobby.cli.linear.LinearSyncService")
    @patch("gobby.cli.linear.get_linear_deps")
    def test_import_with_team(
        self, mock_deps: MagicMock, mock_svc: MagicMock, mock_async: MagicMock, runner: CliRunner
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        mock_async.return_value = [
            {"id": "t1", "title": "Issue 1"},
            {"id": "t2", "title": "Issue 2"},
        ]
        result = runner.invoke(linear, ["import", "TEAM-1"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "2 issues" in result.output

    @patch("gobby.cli.linear.asyncio.run")
    @patch("gobby.cli.linear.LinearSyncService")
    @patch("gobby.cli.linear.get_linear_deps")
    def test_import_json(
        self, mock_deps: MagicMock, mock_svc: MagicMock, mock_async: MagicMock, runner: CliRunner
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        mock_async.return_value = [{"id": "t1", "title": "Issue 1"}]
        result = runner.invoke(linear, ["import", "TEAM-1", "--json"], catch_exceptions=False)
        assert result.exit_code == 0
        assert '"count": 1' in result.output

    @patch("gobby.cli.linear.get_linear_deps")
    def test_import_no_team(self, mock_deps: MagicMock, runner: CliRunner) -> None:
        tm, mcp, pm, pid = _mock_linear_deps(linear_team_id=None)
        mock_deps.return_value = (tm, mcp, pm, pid)
        result = runner.invoke(linear, ["import"], catch_exceptions=False)
        assert result.exit_code != 0

    @patch("gobby.cli.linear.asyncio.run")
    @patch("gobby.cli.linear.LinearSyncService")
    @patch("gobby.cli.linear.get_linear_deps")
    def test_import_with_labels_and_state(
        self, mock_deps: MagicMock, mock_svc: MagicMock, mock_async: MagicMock, runner: CliRunner
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps(linear_team_id="TEAM-1")
        mock_deps.return_value = (tm, mcp, pm, pid)
        mock_async.return_value = []
        result = runner.invoke(
            linear,
            ["import", "--state", "Todo", "--labels", "bug,urgent"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# linear sync
# ---------------------------------------------------------------------------
class TestLinearSync:
    @patch("gobby.cli.linear.asyncio.run", return_value={"synced": True})
    @patch("gobby.cli.linear.get_sync_service")
    @patch("gobby.cli.linear.resolve_task_id")
    @patch("gobby.cli.linear.get_linear_deps")
    def test_sync_text(
        self,
        mock_deps: MagicMock,
        mock_resolve: MagicMock,
        mock_svc: MagicMock,
        _async: MagicMock,
        runner: CliRunner,
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        task = MagicMock()
        task.id = "task-uuid"
        mock_resolve.return_value = task
        result = runner.invoke(linear, ["sync", "#1"], catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.cli.linear.asyncio.run", return_value={"synced": True})
    @patch("gobby.cli.linear.get_sync_service")
    @patch("gobby.cli.linear.resolve_task_id")
    @patch("gobby.cli.linear.get_linear_deps")
    def test_sync_json(
        self,
        mock_deps: MagicMock,
        mock_resolve: MagicMock,
        mock_svc: MagicMock,
        _async: MagicMock,
        runner: CliRunner,
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        task = MagicMock()
        task.id = "task-uuid"
        mock_resolve.return_value = task
        result = runner.invoke(linear, ["sync", "#1", "--json"], catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.cli.linear.resolve_task_id", return_value=None)
    @patch("gobby.cli.linear.get_linear_deps")
    def test_sync_task_not_found(
        self, mock_deps: MagicMock, _resolve: MagicMock, runner: CliRunner
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        result = runner.invoke(linear, ["sync", "bad-id"], catch_exceptions=False)
        assert result.exit_code == 0  # resolve returns None, command returns early


# ---------------------------------------------------------------------------
# linear create
# ---------------------------------------------------------------------------
class TestLinearCreate:
    @patch("gobby.cli.linear.asyncio.run", return_value={"id": "LIN-123"})
    @patch("gobby.cli.linear.get_sync_service")
    @patch("gobby.cli.linear.resolve_task_id")
    @patch("gobby.cli.linear.get_linear_deps")
    def test_create_text(
        self,
        mock_deps: MagicMock,
        mock_resolve: MagicMock,
        mock_svc: MagicMock,
        _async: MagicMock,
        runner: CliRunner,
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        task = MagicMock()
        task.id = "task-uuid"
        mock_resolve.return_value = task
        result = runner.invoke(linear, ["create", "#1"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "LIN-123" in result.output

    @patch("gobby.cli.linear.asyncio.run", return_value={"id": "LIN-123"})
    @patch("gobby.cli.linear.get_sync_service")
    @patch("gobby.cli.linear.resolve_task_id")
    @patch("gobby.cli.linear.get_linear_deps")
    def test_create_json(
        self,
        mock_deps: MagicMock,
        mock_resolve: MagicMock,
        mock_svc: MagicMock,
        _async: MagicMock,
        runner: CliRunner,
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        task = MagicMock()
        task.id = "task-uuid"
        mock_resolve.return_value = task
        result = runner.invoke(linear, ["create", "#1", "--json"], catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.cli.linear.resolve_task_id", return_value=None)
    @patch("gobby.cli.linear.get_linear_deps")
    def test_create_task_not_found(
        self, mock_deps: MagicMock, _resolve: MagicMock, runner: CliRunner
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        result = runner.invoke(linear, ["create", "bad"], catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.cli.linear.asyncio.run", side_effect=ValueError("bad task"))
    @patch("gobby.cli.linear.get_sync_service")
    @patch("gobby.cli.linear.resolve_task_id")
    @patch("gobby.cli.linear.get_linear_deps")
    def test_create_value_error(
        self,
        mock_deps: MagicMock,
        mock_resolve: MagicMock,
        mock_svc: MagicMock,
        _async: MagicMock,
        runner: CliRunner,
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        task = MagicMock()
        task.id = "task-uuid"
        mock_resolve.return_value = task
        result = runner.invoke(linear, ["create", "#1"], catch_exceptions=False)
        assert result.exit_code != 0
