"""Tests for cli/github.py — targeting uncovered lines."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.github import github

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _mock_github_deps(
    project_id: str = "proj-123",
    github_repo: str | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock, str]:
    db = MagicMock()
    task_manager = MagicMock()
    task_manager.db = db
    project_manager = MagicMock()
    project = MagicMock()
    project.github_repo = github_repo
    project_manager.get.return_value = project
    mcp_manager = MagicMock()
    return task_manager, mcp_manager, project_manager, project_id


# ---------------------------------------------------------------------------
# github status
# ---------------------------------------------------------------------------
class TestGithubStatus:
    @patch("gobby.cli.github.GitHubIntegration")
    @patch("gobby.cli.github.get_github_deps")
    def test_status_text(self, mock_deps: MagicMock, mock_gh: MagicMock, runner: CliRunner) -> None:
        tm, mcp, pm, pid = _mock_github_deps(github_repo="owner/repo")
        mock_deps.return_value = (tm, mcp, pm, pid)
        tm.db.fetchone.return_value = {"count": 3}
        mock_gh.return_value.is_available.return_value = True
        result = runner.invoke(github, ["status"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "owner/repo" in result.output

    @patch("gobby.cli.github.GitHubIntegration")
    @patch("gobby.cli.github.get_github_deps")
    def test_status_json(self, mock_deps: MagicMock, mock_gh: MagicMock, runner: CliRunner) -> None:
        tm, mcp, pm, pid = _mock_github_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        tm.db.fetchone.return_value = {"count": 0}
        mock_gh.return_value.is_available.return_value = False
        mock_gh.return_value.get_unavailable_reason.return_value = "No token"
        result = runner.invoke(github, ["status", "--json"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No token" in result.output

    @patch("gobby.cli.github.get_github_deps", side_effect=Exception("fail"))
    def test_status_exception(self, _deps: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(github, ["status"], catch_exceptions=False)
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# github link / unlink
# ---------------------------------------------------------------------------
class TestGithubLink:
    @patch("gobby.cli.github.get_github_deps")
    def test_link_valid(self, mock_deps: MagicMock, runner: CliRunner) -> None:
        tm, mcp, pm, pid = _mock_github_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        result = runner.invoke(github, ["link", "owner/repo"], catch_exceptions=False)
        assert result.exit_code == 0
        pm.update.assert_called_once_with(pid, github_repo="owner/repo")

    @patch("gobby.cli.github.get_github_deps")
    def test_link_invalid_format(self, mock_deps: MagicMock, runner: CliRunner) -> None:
        tm, mcp, pm, pid = _mock_github_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        result = runner.invoke(github, ["link", "noslash"], catch_exceptions=False)
        assert result.exit_code != 0

    @patch("gobby.cli.github.get_github_deps")
    def test_link_too_many_slashes(self, mock_deps: MagicMock, runner: CliRunner) -> None:
        tm, mcp, pm, pid = _mock_github_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        result = runner.invoke(github, ["link", "a/b/c"], catch_exceptions=False)
        assert result.exit_code != 0

    @patch("gobby.cli.github.get_github_deps", side_effect=Exception("boom"))
    def test_link_error(self, _deps: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(github, ["link", "owner/repo"], catch_exceptions=False)
        assert result.exit_code != 0


class TestGithubUnlink:
    @patch("gobby.cli.github.get_github_deps")
    def test_unlink(self, mock_deps: MagicMock, runner: CliRunner) -> None:
        tm, mcp, pm, pid = _mock_github_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        result = runner.invoke(github, ["unlink"], catch_exceptions=False)
        assert result.exit_code == 0
        pm.update.assert_called_once_with(pid, github_repo=None)

    @patch("gobby.cli.github.get_github_deps", side_effect=Exception("boom"))
    def test_unlink_error(self, _deps: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(github, ["unlink"], catch_exceptions=False)
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# github import
# ---------------------------------------------------------------------------
class TestGithubImport:
    @patch("gobby.cli.github.asyncio.run")
    @patch("gobby.cli.github.GitHubSyncService")
    @patch("gobby.cli.github.get_github_deps")
    def test_import_with_repo(
        self, mock_deps: MagicMock, mock_svc: MagicMock, mock_async: MagicMock, runner: CliRunner
    ) -> None:
        tm, mcp, pm, pid = _mock_github_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        mock_async.return_value = [
            {"id": "t1", "title": "Issue 1"},
        ]
        result = runner.invoke(github, ["import", "owner/repo"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "1 issues" in result.output

    @patch("gobby.cli.github.asyncio.run")
    @patch("gobby.cli.github.GitHubSyncService")
    @patch("gobby.cli.github.get_github_deps")
    def test_import_json(
        self, mock_deps: MagicMock, mock_svc: MagicMock, mock_async: MagicMock, runner: CliRunner
    ) -> None:
        tm, mcp, pm, pid = _mock_github_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        mock_async.return_value = []
        result = runner.invoke(github, ["import", "owner/repo", "--json"], catch_exceptions=False)
        assert result.exit_code == 0
        assert '"count": 0' in result.output

    @patch("gobby.cli.github.get_github_deps")
    def test_import_no_repo(self, mock_deps: MagicMock, runner: CliRunner) -> None:
        tm, mcp, pm, pid = _mock_github_deps(github_repo=None)
        mock_deps.return_value = (tm, mcp, pm, pid)
        result = runner.invoke(github, ["import"], catch_exceptions=False)
        assert result.exit_code != 0

    @patch("gobby.cli.github.asyncio.run")
    @patch("gobby.cli.github.GitHubSyncService")
    @patch("gobby.cli.github.get_github_deps")
    def test_import_with_labels_state(
        self, mock_deps: MagicMock, mock_svc: MagicMock, mock_async: MagicMock, runner: CliRunner
    ) -> None:
        tm, mcp, pm, pid = _mock_github_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        mock_async.return_value = []
        result = runner.invoke(
            github,
            ["import", "owner/repo", "--labels", "bug,help", "--state", "closed"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        mock_async.assert_called_once()
        mock_svc.assert_called_once()


# ---------------------------------------------------------------------------
# github sync
# ---------------------------------------------------------------------------
class TestGithubSync:
    @patch("gobby.cli.github.asyncio.run", return_value={"ok": True})
    @patch("gobby.cli.github.get_sync_service")
    def test_sync_text(self, mock_svc: MagicMock, _async: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(github, ["sync", "task-uuid"], catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.cli.github.asyncio.run", return_value={"ok": True})
    @patch("gobby.cli.github.get_sync_service")
    def test_sync_json(self, mock_svc: MagicMock, _async: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(github, ["sync", "task-uuid", "--json"], catch_exceptions=False)
        assert result.exit_code == 0

    @patch("gobby.cli.github.asyncio.run", side_effect=ValueError("bad"))
    @patch("gobby.cli.github.get_sync_service")
    def test_sync_value_error(
        self, mock_svc: MagicMock, _async: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(github, ["sync", "task-uuid"], catch_exceptions=False)
        assert result.exit_code != 0

    @patch("gobby.cli.github.asyncio.run", side_effect=RuntimeError("fail"))
    @patch("gobby.cli.github.get_sync_service")
    def test_sync_generic_error(
        self, mock_svc: MagicMock, _async: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(github, ["sync", "task-uuid"], catch_exceptions=False)
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# github pr
# ---------------------------------------------------------------------------
class TestGithubPr:
    @patch(
        "gobby.cli.github.asyncio.run",
        return_value={"number": 42, "html_url": "https://github.com/pr/42"},
    )
    @patch("gobby.cli.github.get_sync_service")
    def test_pr_text(self, mock_svc: MagicMock, _async: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(
            github,
            ["pr", "task-uuid", "--head", "feature-branch"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "#42" in result.output
        assert "https://github.com/pr/42" in result.output

    @patch(
        "gobby.cli.github.asyncio.run",
        return_value={"number": 1, "url": "https://api.github.com/pr/1"},
    )
    @patch("gobby.cli.github.get_sync_service")
    def test_pr_json(self, mock_svc: MagicMock, _async: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(
            github,
            ["pr", "task-uuid", "--head", "feat", "--json"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0

    @patch("gobby.cli.github.asyncio.run", return_value={"number": 1})
    @patch("gobby.cli.github.get_sync_service")
    def test_pr_no_url(self, mock_svc: MagicMock, _async: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(
            github,
            ["pr", "task-uuid", "--head", "feat"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "#1" in result.output

    @patch("gobby.cli.github.asyncio.run", side_effect=ValueError("no task"))
    @patch("gobby.cli.github.get_sync_service")
    def test_pr_value_error(
        self, mock_svc: MagicMock, _async: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            github,
            ["pr", "task-uuid", "--head", "feat"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0

    @patch("gobby.cli.github.asyncio.run", side_effect=RuntimeError("fail"))
    @patch("gobby.cli.github.get_sync_service")
    def test_pr_generic_error(
        self, mock_svc: MagicMock, _async: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(
            github,
            ["pr", "task-uuid", "--head", "feat"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
