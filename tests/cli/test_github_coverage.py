"""Tests for cli/github.py — targeting uncovered lines."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.github import _gather_github_access, github
from gobby.storage.github_triage import GitHubTriageConfig
from gobby.sync.github_issue_sync import GitHubRepositoryReadinessError

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.mark.asyncio
async def test_gather_github_access_isolates_per_repository_failures() -> None:
    with patch(
        "gobby.cli.github._check_github_access_result",
        new=AsyncMock(side_effect=[RuntimeError("boom"), (("owner/repo",), None)]),
    ):
        results = await _gather_github_access(
            [
                (MagicMock(), MagicMock(), MagicMock(), MagicMock()),
                (MagicMock(), MagicMock(), MagicMock(), MagicMock()),
            ]
        )

    assert len(results) == 2
    assert results == [(None, "boom"), (("owner/repo",), None)]


def _mock_github_deps(
    project_id: str = "proj-123",
    github_repo: str | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock, str]:
    db = MagicMock()
    task_manager = MagicMock()
    task_manager.db = db
    project_manager = MagicMock()
    project = MagicMock()
    project.id = project_id
    project.name = "gobby"
    project.github_repo = github_repo
    project.deleted_at = None
    project_manager.get.return_value = project
    mcp_manager = MagicMock()
    mcp_manager.disconnect_all = AsyncMock()
    return task_manager, mcp_manager, project_manager, project_id


# ---------------------------------------------------------------------------
# github status
# ---------------------------------------------------------------------------
class TestGithubStatus:
    @patch("gobby.cli.github.GitHubIssueSyncService")
    @patch("gobby.cli.github.ExternalIssueSyncStatusStore")
    @patch("gobby.cli.github.GitHubTriageStore")
    @patch("gobby.cli.github.get_github_deps")
    def test_status_text(
        self,
        mock_deps: MagicMock,
        mock_config_store: MagicMock,
        mock_status_store: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
    ) -> None:
        tm, mcp, pm, pid = _mock_github_deps(github_repo="owner/repo")
        mock_deps.return_value = (tm, mcp, pm, pid)
        config = GitHubTriageConfig(
            project_id=pid,
            sync_enabled=True,
            repositories=("owner/repo",),
        )
        mock_config_store.return_value.get_config.return_value = config
        mock_status_store.return_value.counts.return_value = (3, 0)
        mock_status_store.return_value.get.return_value = None
        mock_sync.return_value.repositories_for.return_value = ("owner/repo",)
        mock_sync.return_value.check_access = AsyncMock(return_value=("owner/repo",))
        result = runner.invoke(github, ["status"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "owner/repo" in result.output

    @patch("gobby.cli.github.GitHubIssueSyncService")
    @patch("gobby.cli.github.ExternalIssueSyncStatusStore")
    @patch("gobby.cli.github.GitHubTriageStore")
    @patch("gobby.cli.github.get_github_deps")
    def test_status_json(
        self,
        mock_deps: MagicMock,
        mock_config_store: MagicMock,
        mock_status_store: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
    ) -> None:
        tm, mcp, pm, pid = _mock_github_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        config = GitHubTriageConfig(project_id=pid, sync_enabled=True)
        mock_config_store.return_value.get_config.return_value = config
        mock_status_store.return_value.counts.return_value = (0, 0)
        mock_status_store.return_value.get.return_value = None
        mock_sync.return_value.repositories_for.return_value = ()
        mock_sync.return_value.check_access = AsyncMock(
            side_effect=GitHubRepositoryReadinessError("No token")
        )
        result = runner.invoke(github, ["status", "--json"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No token" in result.output
        assert json.loads(result.output)["last_outbound_success_at"] is None

    @patch("gobby.cli.github.GitHubIssueSyncService")
    @patch("gobby.cli.github.ExternalIssueSyncStatusStore")
    @patch("gobby.cli.github.GitHubTriageStore")
    @patch("gobby.cli.github.get_github_deps")
    def test_status_skips_readiness_when_integration_is_disabled(
        self,
        mock_deps: MagicMock,
        mock_config_store: MagicMock,
        mock_status_store: MagicMock,
        mock_sync: MagicMock,
        runner: CliRunner,
    ) -> None:
        tm, mcp, pm, pid = _mock_github_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        config = GitHubTriageConfig(
            project_id=pid,
            repositories=("owner/repo",),
        )
        mock_config_store.return_value.get_config.return_value = config
        mock_status_store.return_value.counts.return_value = (0, 0)
        mock_status_store.return_value.get.return_value = None
        mock_sync.return_value.repositories_for.return_value = ("owner/repo",)
        check_access = AsyncMock()
        mock_sync.return_value.check_access = check_access

        result = runner.invoke(github, ["status"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Ready: ✗" in result.output
        assert "owner/repo" in result.output
        mock_sync.return_value.repositories_for.assert_called_once_with(
            pm.get.return_value,
            config,
        )
        check_access.assert_not_awaited()

    @patch("gobby.cli.github.MCPClientManager")
    @patch("gobby.cli.github.GitHubIssueSyncService")
    @patch("gobby.cli.github.ExternalIssueSyncStatusStore")
    @patch("gobby.cli.github.GitHubTriageStore")
    @patch("gobby.cli.github.get_github_deps")
    def test_status_gathers_enabled_project_readiness_in_one_event_loop(
        self,
        mock_deps: MagicMock,
        mock_config_store: MagicMock,
        mock_status_store: MagicMock,
        mock_sync: MagicMock,
        mock_mcp_type: MagicMock,
        runner: CliRunner,
    ) -> None:
        tm, mcp, pm, _ = _mock_github_deps()
        first = MagicMock(id="project-1", name="one", deleted_at=None)
        second = MagicMock(id="project-2", name="two", deleted_at=None)
        pm.list.return_value = [first, second]
        mock_deps.return_value = (tm, mcp, pm, "")
        mock_config_store.return_value.get_config.side_effect = [
            GitHubTriageConfig(
                project_id="project-1",
                sync_enabled=True,
                repositories=("owner/one",),
            ),
            GitHubTriageConfig(
                project_id="project-2",
                triage_enabled=True,
                repositories=("owner/two",),
            ),
        ]
        mock_status_store.return_value.counts.return_value = (0, 0)
        mock_status_store.return_value.get.return_value = None
        mock_sync.return_value.repositories_for.side_effect = [
            ("owner/one",),
            ("owner/two",),
        ]
        check_access = AsyncMock(side_effect=[("owner/one",), ("owner/two",)])
        mock_sync.return_value.check_access = check_access
        mock_mcp_type.return_value.disconnect_all = AsyncMock()

        with patch("gobby.cli.github.asyncio.run", wraps=asyncio.run) as run:
            result = runner.invoke(github, ["status", "--all"], catch_exceptions=False)

        assert result.exit_code == 0
        run.assert_called_once()
        assert check_access.await_count == 2

    @patch("gobby.cli.github.get_github_deps")
    def test_status_json_returns_empty_list_when_project_is_absent(
        self,
        mock_deps: MagicMock,
        runner: CliRunner,
    ) -> None:
        tm, mcp, pm, pid = _mock_github_deps()
        pm.get.return_value = None
        mock_deps.return_value = (tm, mcp, pm, pid)

        result = runner.invoke(github, ["status", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        assert json.loads(result.output) == []

    @patch("gobby.cli.github.get_github_deps", side_effect=Exception("fail"))
    def test_status_exception(self, _deps: MagicMock, runner: CliRunner) -> None:
        result = runner.invoke(github, ["status"], catch_exceptions=False)
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# github link / unlink
# ---------------------------------------------------------------------------
class TestGithubSetup:
    @patch("gobby.cli.github.GitHubIssueSyncService")
    @patch("gobby.cli.github.GitHubTriageStore")
    @patch("gobby.cli.github.get_github_deps")
    def test_setup_controls_sync_and_triage_independently(
        self,
        mock_deps: MagicMock,
        mock_store_type: MagicMock,
        mock_sync_type: MagicMock,
        runner: CliRunner,
    ) -> None:
        tm, mcp, pm, pid = _mock_github_deps()
        mock_deps.return_value = (tm, mcp, pm, pid)
        store = mock_store_type.return_value
        store.get_config.return_value = GitHubTriageConfig(project_id=pid)
        store.upsert_config.side_effect = lambda config: config
        mock_sync_type.return_value.check_access = AsyncMock(return_value=("owner/repo",))

        result = runner.invoke(
            github,
            [
                "setup",
                "--project",
                "gobby",
                "--repo",
                "owner/repo",
                "--sync",
                "--no-triage",
                "--json",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        saved = store.upsert_config.call_args.args[0]
        assert saved.sync_enabled is True
        assert saved.triage_enabled is False
        assert saved.repositories == ("owner/repo",)
        mcp.disconnect_all.assert_awaited_once()
        mock_deps.assert_called_once_with("gobby")

    @patch("gobby.cli.github.GitHubIssueSyncService")
    @patch("gobby.cli.github.GitHubTriageStore")
    @patch("gobby.cli.github.get_github_deps")
    def test_setup_fails_when_repository_is_inaccessible(
        self,
        mock_deps: MagicMock,
        mock_store_type: MagicMock,
        mock_sync_type: MagicMock,
        runner: CliRunner,
    ) -> None:
        tm, mcp, pm, pid = _mock_github_deps(github_repo="private/repo")
        mock_deps.return_value = (tm, mcp, pm, pid)
        store = mock_store_type.return_value
        store.get_config.return_value = GitHubTriageConfig(project_id=pid)
        mock_sync_type.return_value.check_access = AsyncMock(
            side_effect=GitHubRepositoryReadinessError("Cannot access private/repo: 404")
        )

        result = runner.invoke(github, ["setup", "--sync"])

        assert result.exit_code != 0
        assert "Cannot access private/repo" in result.output
        store.upsert_config.assert_not_called()


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
