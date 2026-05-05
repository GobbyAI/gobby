"""Tests for cli/linear.py — targeting uncovered lines."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.linear import _run_linear_setup, linear

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _mock_linear_deps(
    project_id: str = "proj-123",
    linear_team_id: str | None = None,
    linear_project_id: str | None = None,
    _github_repo: str | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock, str]:
    """Build a mock tuple for get_linear_deps."""
    db = MagicMock()
    task_manager = MagicMock()
    task_manager.db = db
    project_manager = MagicMock()
    project = MagicMock()
    project.linear_team_id = linear_team_id
    project.linear_project_id = linear_project_id
    project.name = "gobby"
    project.repo_path = "/tmp/gobby"
    project_manager.get.return_value = project
    mcp_manager = MagicMock()
    mcp_manager.has_server.return_value = True
    mcp_manager.health = {"linear": MagicMock(state="connected")}
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
        pm.update.assert_called_once_with(pid, linear_team_id="TEAM-42", linear_project_id=None)

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
        pm.update.assert_called_once_with(pid, linear_team_id=None, linear_project_id=None)

    @patch("gobby.cli.linear.get_linear_deps", side_effect=Exception("boom"))
    def test_unlink_error(self, _deps: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(linear, ["unlink"], catch_exceptions=False)
        assert result.exit_code != 0


class TestLinearTeams:
    @patch("gobby.cli.linear.asyncio.run")
    @patch("gobby.cli.linear.LinearSyncService")
    @patch("gobby.cli.linear.get_linear_deps")
    def test_teams_text(
        self, mock_deps: MagicMock, _svc: MagicMock, mock_async: MagicMock, runner: CliRunner
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        mock_async.return_value = [{"id": "team-1", "name": "Engineering", "key": "ENG"}]

        result = runner.invoke(linear, ["teams"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Engineering" in result.output
        assert "ENG" in result.output


class TestLinearSetup:
    @pytest.mark.asyncio
    async def test_setup_auto_selects_single_team_and_stores_binding(self) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        tm.db.fetchall.return_value = []
        mcp.call_tool = AsyncMock(
            side_effect=[
                {"teams": [{"id": "team-1", "name": "Engineering", "key": "ENG"}]},
                {"projects": [{"id": "lin-proj", "name": "gobby"}]},
            ]
        )

        result = await _run_linear_setup(
            task_manager=tm,
            mcp_manager=mcp,
            project_manager=pm,
            project_id=pid,
            bootstrap=True,
            team_id=None,
            linear_project_id=None,
            project_name=None,
            import_issues=False,
            create_missing=False,
        )

        assert result["linear_team_id"] == "team-1"
        assert result["linear_project_id"] == "lin-proj"
        pm.update.assert_any_call(pid, linear_team_id="team-1", linear_project_id="lin-proj")

    @pytest.mark.asyncio
    async def test_setup_create_missing_uses_forward_active_sync(self) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        tm.db.fetchall.return_value = []
        mcp.call_tool = AsyncMock(
            side_effect=[
                {"teams": [{"id": "team-1", "name": "Engineering", "key": "ENG"}]},
                {"projects": [{"id": "lin-proj", "name": "gobby"}]},
            ]
        )

        result = await _run_linear_setup(
            task_manager=tm,
            mcp_manager=mcp,
            project_manager=pm,
            project_id=pid,
            bootstrap=True,
            team_id=None,
            linear_project_id=None,
            project_name=None,
            import_issues=False,
            create_missing=True,
        )

        assert result["sync"]["mode"] == "forward_active"
        assert result["created_missing_count"] == 0

    @pytest.mark.asyncio
    async def test_setup_multiple_teams_requires_team_id(self) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        mcp.call_tool = AsyncMock(
            return_value={
                "teams": [
                    {"id": "team-1", "name": "Engineering"},
                    {"id": "team-2", "name": "Design"},
                ]
            }
        )

        with pytest.raises(Exception, match="--team-id"):
            await _run_linear_setup(
                task_manager=tm,
                mcp_manager=mcp,
                project_manager=pm,
                project_id=pid,
                bootstrap=True,
                team_id=None,
                linear_project_id=None,
                project_name=None,
                import_issues=False,
                create_missing=False,
            )

    @pytest.mark.asyncio
    async def test_setup_no_teams_returns_actionable_error(self) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        mcp.call_tool = AsyncMock(return_value={"teams": []})

        with pytest.raises(Exception, match="No Linear teams"):
            await _run_linear_setup(
                task_manager=tm,
                mcp_manager=mcp,
                project_manager=pm,
                project_id=pid,
                bootstrap=True,
                team_id=None,
                linear_project_id=None,
                project_name=None,
                import_issues=False,
                create_missing=False,
            )

    @patch("gobby.cli.linear.asyncio.run")
    @patch("gobby.cli.linear.get_linear_deps")
    def test_setup_cli_json(
        self, mock_deps: MagicMock, mock_async: MagicMock, runner: CliRunner
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        mock_async.return_value = {
            "project_id": pid,
            "linear_team_id": "team-1",
            "linear_project_id": "lin-proj",
            "linear_project_name": "gobby",
            "created_linear_project": False,
            "imported_count": 0,
            "created_missing_count": 0,
            "sync": {"pull": {}, "push": {}},
        }

        result = runner.invoke(linear, ["setup", "--bootstrap", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        assert '"linear_project_id": "lin-proj"' in result.output


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

    @patch(
        "gobby.cli.linear.asyncio.run",
        return_value={
            "mode": "forward_active",
            "created_count": 3,
            "created_issues": [],
            "push": {"pushed": 5, "skipped": 0, "errors": 0},
            "synced_at": "2026-05-05T00:00:00+00:00",
        },
    )
    @patch("gobby.cli.linear.get_sync_service")
    @patch("gobby.cli.linear.get_linear_deps")
    def test_sync_all_forward_active_text(
        self,
        mock_deps: MagicMock,
        mock_svc: MagicMock,
        _async: MagicMock,
        runner: CliRunner,
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps(linear_team_id="TEAM-1")
        mock_deps.return_value = (tm, mcp, pm, pid)

        result = runner.invoke(linear, ["sync-all", "--forward"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Forward active Linear sync complete" in result.output
        assert "Created missing issues: 3" in result.output
        mock_svc.return_value.sync_active_forward.assert_called_once_with(team_id="TEAM-1")

    @patch(
        "gobby.cli.linear.asyncio.run",
        return_value={
            "mode": "forward_active",
            "created_count": 1,
            "created_issues": [],
            "push": {"pushed": 1, "skipped": 0, "errors": 0},
            "synced_at": "2026-05-05T00:00:00+00:00",
        },
    )
    @patch("gobby.cli.linear.get_sync_service")
    @patch("gobby.cli.linear.get_linear_deps")
    def test_sync_all_forward_active_alias_still_works(
        self,
        mock_deps: MagicMock,
        mock_svc: MagicMock,
        _async: MagicMock,
        runner: CliRunner,
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps(linear_team_id="TEAM-1")
        mock_deps.return_value = (tm, mcp, pm, pid)

        result = runner.invoke(
            linear, ["sync-all", "--forward", "--active"], catch_exceptions=False
        )

        assert result.exit_code == 0
        assert "Forward active Linear sync complete" in result.output
        mock_svc.return_value.sync_active_forward.assert_called_once_with(team_id="TEAM-1")

    @patch("gobby.cli.linear.get_linear_deps")
    def test_sync_all_active_requires_forward(
        self, mock_deps: MagicMock, runner: CliRunner
    ) -> None:
        tm, mcp, pm, pid = _mock_linear_deps(linear_team_id="TEAM-1")
        mock_deps.return_value = (tm, mcp, pm, pid)

        result = runner.invoke(linear, ["sync-all", "--active"], catch_exceptions=False)

        assert result.exit_code != 0
        assert "--active is only supported with --forward" in result.output


# ---------------------------------------------------------------------------
# linear create
# ---------------------------------------------------------------------------
class TestLinearCreate:
    @patch(
        "gobby.cli.linear.asyncio.run",
        return_value={
            "id": "lin-uuid",
            "gobby_ref": "#1",
            "linear_identifier": "GOB-99",
            "linear_project_name": "gobby",
        },
    )
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
        assert "Registered #1 in Linear project gobby" in result.output
        assert "GOB-99" in result.output

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
