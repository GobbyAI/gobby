"""Tests for cli/worktrees.py -- targeting uncovered lines.

Covers: create error paths, list json/empty, show not-found/json, delete errors,
        release, sync errors, stale, cleanup, stats, resolve_worktree_id.
Lines targeted: 82-112, 131-166, 188-264, 278-481
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import click
import httpx
import pytest
from click.testing import CliRunner

from gobby.cli.worktrees import resolve_worktree_id, worktrees
from gobby.storage.worktrees import Worktree

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _make_worktree(**overrides: Any) -> Worktree:
    defaults = {
        "id": "wt-aaa-bbb-ccc-ddd",
        "branch_name": "feat/x",
        "status": "active",
        "worktree_path": "/tmp/wt",
        "base_branch": "main",
        "project_id": "proj-1",
        "task_id": None,
        "agent_session_id": None,
        "created_at": "2024-01-01",
        "updated_at": "2024-01-02",
        "merged_at": None,
    }
    defaults.update(overrides)
    return Worktree(**defaults)


# =============================================================================
# create_worktree error paths
# =============================================================================


class TestCreateWorktreeErrors:
    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post", side_effect=httpx.ConnectError("refused"))
    def test_create_connect_error(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        with patch("os.getcwd", return_value="/app"):
            result = runner.invoke(worktrees, ["create", "feat/x"])
        assert result.exit_code == 0
        assert "Cannot connect to Gobby daemon" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post")
    def test_create_http_error(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        resp = MagicMock()
        resp.status_code = 500
        resp.text = "Internal Server Error"
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=resp
        )
        mock_post.return_value = resp
        with patch("os.getcwd", return_value="/app"):
            result = runner.invoke(worktrees, ["create", "feat/x"])
        assert result.exit_code != 0
        assert "HTTP Error 500" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post", side_effect=RuntimeError("boom"))
    def test_create_generic_error(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        with patch("os.getcwd", return_value="/app"):
            result = runner.invoke(worktrees, ["create", "feat/x"])
        assert "Error: boom" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post")
    def test_create_json_output(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        resp = MagicMock()
        resp.json.return_value = {"success": True, "worktree_id": "wt-1"}
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp
        with patch("os.getcwd", return_value="/app"):
            result = runner.invoke(worktrees, ["create", "feat/x", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["success"] is True

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post")
    def test_create_failure_result(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        resp = MagicMock()
        resp.json.return_value = {"success": False, "error": "branch exists"}
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp
        with patch("os.getcwd", return_value="/app"):
            result = runner.invoke(worktrees, ["create", "feat/x"])
        assert "Failed to create worktree" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("gobby.cli.worktrees.resolve_task_id", return_value=None)
    @patch("gobby.cli.worktrees.get_task_manager")
    def test_create_task_not_resolved(
        self, mock_tm: MagicMock, mock_resolve: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        with patch("os.getcwd", return_value="/app"):
            result = runner.invoke(worktrees, ["create", "feat/x", "--task", "#999"])
        assert result.exit_code == 0  # early return, no error exit


# =============================================================================
# list_worktrees
# =============================================================================


class TestListWorktrees:
    @patch("gobby.cli.worktrees.get_worktree_manager")
    def test_list_json(self, mock_mgr: MagicMock, runner: CliRunner) -> None:
        wt = _make_worktree()
        mock_mgr.return_value.list_worktrees.return_value = [wt]
        result = runner.invoke(worktrees, ["list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1

    @patch("gobby.cli.worktrees.get_worktree_manager")
    def test_list_empty(self, mock_mgr: MagicMock, runner: CliRunner) -> None:
        mock_mgr.return_value.list_worktrees.return_value = []
        result = runner.invoke(worktrees, ["list"])
        assert result.exit_code == 0
        assert "No worktrees found" in result.output

    @patch("gobby.cli.worktrees.get_worktree_manager")
    def test_list_with_session(self, mock_mgr: MagicMock, runner: CliRunner) -> None:
        wt = _make_worktree(agent_session_id="sess-123456789")
        mock_mgr.return_value.list_worktrees.return_value = [wt]
        result = runner.invoke(worktrees, ["list"])
        assert result.exit_code == 0
        assert "sess-123" in result.output

    @patch("gobby.cli.worktrees.resolve_project_ref", return_value="proj-1")
    @patch("gobby.cli.worktrees.get_worktree_manager")
    def test_list_with_project_filter(
        self, mock_mgr: MagicMock, mock_proj: MagicMock, runner: CliRunner
    ) -> None:
        mock_mgr.return_value.list_worktrees.return_value = []
        result = runner.invoke(worktrees, ["list", "--project", "my-proj"])
        assert result.exit_code == 0
        mock_mgr.return_value.list_worktrees.assert_called_once_with(
            status=None, project_id="proj-1"
        )


# =============================================================================
# show_worktree
# =============================================================================


class TestShowWorktree:
    @patch("gobby.cli.worktrees.resolve_worktree_id", return_value="wt-123")
    @patch("gobby.cli.worktrees.get_worktree_manager")
    def test_show_not_found(
        self, mock_mgr: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        mock_mgr.return_value.get.return_value = None
        result = runner.invoke(worktrees, ["show", "wt-123"])
        assert "Worktree not found" in result.output

    @patch("gobby.cli.worktrees.resolve_worktree_id", return_value="wt-123")
    @patch("gobby.cli.worktrees.get_worktree_manager")
    def test_show_json(
        self, mock_mgr: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        wt = _make_worktree(id="wt-123")
        mock_mgr.return_value.get.return_value = wt
        result = runner.invoke(worktrees, ["show", "wt-123", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == "wt-123"

    @patch("gobby.cli.worktrees.resolve_worktree_id", return_value="wt-123")
    @patch("gobby.cli.worktrees.get_worktree_manager")
    def test_show_with_all_fields(
        self, mock_mgr: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        wt = _make_worktree(id="wt-123", agent_session_id="sess-abc", project_id="proj-1")
        mock_mgr.return_value.get.return_value = wt
        result = runner.invoke(worktrees, ["show", "wt-123"])
        assert "Project: proj-1" in result.output
        assert "Session: sess-abc" in result.output


# =============================================================================
# delete_worktree error paths
# =============================================================================


class TestDeleteWorktreeErrors:
    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("gobby.cli.worktrees.resolve_worktree_id", return_value="wt-123")
    @patch("gobby.cli.worktrees.get_worktree_manager")
    @patch("httpx.post", side_effect=httpx.ConnectError("refused"))
    def test_delete_connect_error(
        self,
        mock_post: MagicMock,
        mock_mgr: MagicMock,
        mock_resolve: MagicMock,
        mock_url: MagicMock,
        runner: CliRunner,
    ) -> None:
        result = runner.invoke(worktrees, ["delete", "wt-123", "--yes"])
        assert "Cannot connect to Gobby daemon" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("gobby.cli.worktrees.resolve_worktree_id", return_value="wt-123")
    @patch("gobby.cli.worktrees.get_worktree_manager")
    @patch("httpx.post")
    def test_delete_http_error(
        self,
        mock_post: MagicMock,
        mock_mgr: MagicMock,
        mock_resolve: MagicMock,
        mock_url: MagicMock,
        runner: CliRunner,
    ) -> None:
        resp = MagicMock()
        resp.status_code = 404
        resp.text = "Not found"
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=resp
        )
        mock_post.return_value = resp
        result = runner.invoke(worktrees, ["delete", "wt-123", "--yes"])
        assert "HTTP Error 404" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("gobby.cli.worktrees.resolve_worktree_id", return_value="wt-123")
    @patch("gobby.cli.worktrees.get_worktree_manager")
    @patch("httpx.post")
    def test_delete_failure_result(
        self,
        mock_post: MagicMock,
        mock_mgr: MagicMock,
        mock_resolve: MagicMock,
        mock_url: MagicMock,
        runner: CliRunner,
    ) -> None:
        resp = MagicMock()
        resp.json.return_value = {"success": False, "error": "active session"}
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp
        result = runner.invoke(worktrees, ["delete", "wt-123", "--yes"])
        assert "Failed to delete worktree" in result.output

    @patch("gobby.cli.worktrees.resolve_worktree_id", side_effect=click.ClickException("not found"))
    @patch("gobby.cli.worktrees.get_worktree_manager")
    def test_delete_resolve_error(
        self, mock_mgr: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(worktrees, ["delete", "wt-bad", "--yes"])
        assert "not found" in result.output


# =============================================================================
# release_worktree
# =============================================================================


class TestReleaseWorktree:
    @patch("gobby.cli.worktrees.resolve_worktree_id", return_value="wt-123")
    @patch("gobby.cli.worktrees.get_worktree_manager")
    def test_release_success(
        self, mock_mgr: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        mock_mgr.return_value.release.return_value = True
        result = runner.invoke(worktrees, ["release", "wt-123"])
        assert result.exit_code == 0
        assert "Released worktree wt-123" in result.output

    @patch("gobby.cli.worktrees.resolve_worktree_id", return_value="wt-123")
    @patch("gobby.cli.worktrees.get_worktree_manager")
    def test_release_failure(
        self, mock_mgr: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        mock_mgr.return_value.release.return_value = False
        result = runner.invoke(worktrees, ["release", "wt-123"])
        assert "Failed to release worktree" in result.output


# =============================================================================
# claim_worktree
# =============================================================================


class TestClaimWorktree:
    @patch("gobby.cli.worktrees.resolve_worktree_id", return_value="wt-123")
    @patch("gobby.cli.worktrees.resolve_session_id", return_value="sess-1")
    @patch("gobby.cli.worktrees.get_worktree_manager")
    def test_claim_failure(
        self, mock_mgr: MagicMock, mock_sess: MagicMock, mock_wt: MagicMock, runner: CliRunner
    ) -> None:
        mock_mgr.return_value.claim.return_value = False
        result = runner.invoke(worktrees, ["claim", "wt-123", "sess-1"])
        assert "Failed to claim worktree" in result.output

    @patch("gobby.cli.worktrees.resolve_worktree_id", return_value="wt-123")
    @patch("gobby.cli.worktrees.resolve_session_id", return_value="sess-1")
    @patch("gobby.cli.worktrees.get_worktree_manager")
    def test_claim_success(
        self, mock_mgr: MagicMock, mock_sess: MagicMock, mock_wt: MagicMock, runner: CliRunner
    ) -> None:
        mock_mgr.return_value.claim.return_value = True
        result = runner.invoke(worktrees, ["claim", "wt-123", "sess-1"])
        assert result.exit_code == 0
        assert "Claimed worktree wt-123" in result.output


# =============================================================================
# sync_worktree
# =============================================================================


class TestSyncWorktree:
    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("gobby.cli.worktrees.resolve_worktree_id", return_value="wt-123")
    @patch("gobby.cli.worktrees.get_worktree_manager")
    @patch("httpx.post", side_effect=httpx.ConnectError("refused"))
    def test_sync_connect_error(
        self,
        mock_post: MagicMock,
        mock_mgr: MagicMock,
        mock_resolve: MagicMock,
        mock_url: MagicMock,
        runner: CliRunner,
    ) -> None:
        result = runner.invoke(worktrees, ["sync", "wt-123"])
        assert "Cannot connect to Gobby daemon" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("gobby.cli.worktrees.resolve_worktree_id", return_value="wt-123")
    @patch("gobby.cli.worktrees.get_worktree_manager")
    @patch("httpx.post")
    def test_sync_json_output(
        self,
        mock_post: MagicMock,
        mock_mgr: MagicMock,
        mock_resolve: MagicMock,
        mock_url: MagicMock,
        runner: CliRunner,
    ) -> None:
        resp = MagicMock()
        resp.json.return_value = {"success": True}
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp
        result = runner.invoke(worktrees, ["sync", "wt-123", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["success"] is True

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("gobby.cli.worktrees.resolve_worktree_id", return_value="wt-123")
    @patch("gobby.cli.worktrees.get_worktree_manager")
    @patch("httpx.post")
    def test_sync_failure_result(
        self,
        mock_post: MagicMock,
        mock_mgr: MagicMock,
        mock_resolve: MagicMock,
        mock_url: MagicMock,
        runner: CliRunner,
    ) -> None:
        resp = MagicMock()
        resp.json.return_value = {"success": False, "error": "conflict"}
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp
        result = runner.invoke(worktrees, ["sync", "wt-123"])
        assert "Failed to sync worktree" in result.output

    @patch("gobby.cli.worktrees.resolve_worktree_id", side_effect=click.ClickException("bad"))
    @patch("gobby.cli.worktrees.get_worktree_manager")
    def test_sync_resolve_error(
        self, mock_mgr: MagicMock, mock_resolve: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(worktrees, ["sync", "wt-bad"])
        assert "bad" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("gobby.cli.worktrees.resolve_worktree_id", return_value="wt-123")
    @patch("gobby.cli.worktrees.get_worktree_manager")
    @patch("httpx.post")
    def test_sync_with_source_branch(
        self,
        mock_post: MagicMock,
        mock_mgr: MagicMock,
        mock_resolve: MagicMock,
        mock_url: MagicMock,
        runner: CliRunner,
    ) -> None:
        resp = MagicMock()
        resp.json.return_value = {"success": True}
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp
        result = runner.invoke(worktrees, ["sync", "wt-123", "--source", "develop"])
        assert result.exit_code == 0
        call_json = mock_post.call_args[1]["json"]
        assert call_json["source_branch"] == "develop"


# =============================================================================
# detect_stale
# =============================================================================


class TestDetectStale:
    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post")
    def test_stale_found(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        resp = MagicMock()
        resp.json.return_value = {
            "stale_worktrees": [
                {"id": "wt-1", "branch_name": "old-feat", "updated_at": "2023-01-01"}
            ]
        }
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp
        result = runner.invoke(worktrees, ["stale"])
        assert result.exit_code == 0
        assert "Found 1 stale worktree" in result.output
        assert "old-feat" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post")
    def test_stale_none_found(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        resp = MagicMock()
        resp.json.return_value = {"stale_worktrees": []}
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp
        result = runner.invoke(worktrees, ["stale"])
        assert result.exit_code == 0
        assert "No stale worktrees found" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post")
    def test_stale_json(self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner) -> None:
        resp = MagicMock()
        resp.json.return_value = {"stale_worktrees": []}
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp
        result = runner.invoke(worktrees, ["stale", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "stale_worktrees" in data

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post", side_effect=httpx.ConnectError("refused"))
    def test_stale_connect_error(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(worktrees, ["stale"])
        assert "Cannot connect to Gobby daemon" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post")
    def test_stale_http_error(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        resp = MagicMock()
        resp.status_code = 500
        resp.text = "err"
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=resp
        )
        mock_post.return_value = resp
        result = runner.invoke(worktrees, ["stale"])
        assert "HTTP Error 500" in result.output


# =============================================================================
# cleanup_worktrees
# =============================================================================


class TestCleanupWorktrees:
    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post")
    def test_cleanup_dry_run(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        resp = MagicMock()
        resp.json.return_value = {"stale_worktrees": [{"id": "wt-1", "branch_name": "old"}]}
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp
        result = runner.invoke(worktrees, ["cleanup", "--dry-run"])
        assert result.exit_code == 0
        assert "Would cleanup 1 stale worktree" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post", side_effect=RuntimeError("oops"))
    def test_cleanup_dry_run_error(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(worktrees, ["cleanup", "--dry-run"])
        assert "Error: oops" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post", side_effect=httpx.ConnectError("refused"))
    def test_cleanup_connect_error(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        result = runner.invoke(worktrees, ["cleanup", "--yes"])
        assert "Cannot connect to Gobby daemon" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post")
    def test_cleanup_failure_result(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        resp = MagicMock()
        resp.json.return_value = {"success": False, "error": "failed"}
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp
        result = runner.invoke(worktrees, ["cleanup", "--yes"])
        assert "Failed to cleanup worktrees" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post")
    def test_cleanup_http_error(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        resp = MagicMock()
        resp.status_code = 500
        resp.text = "err"
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=resp
        )
        mock_post.return_value = resp
        result = runner.invoke(worktrees, ["cleanup", "--yes"])
        assert "HTTP Error 500" in result.output


# =============================================================================
# worktree_stats
# =============================================================================


class TestWorktreeStats:
    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post")
    def test_stats_success(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        resp = MagicMock()
        resp.json.return_value = {
            "total": 5,
            "counts": {"active": 3, "stale": 1, "merged": 1, "abandoned": 0, "with_sessions": 2},
        }
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp
        with patch("os.getcwd", return_value="/app"):
            result = runner.invoke(worktrees, ["stats"])
        assert result.exit_code == 0
        assert "Total: 5" in result.output
        assert "Active: 3" in result.output
        assert "With Sessions: 2" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post")
    def test_stats_json(self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner) -> None:
        resp = MagicMock()
        resp.json.return_value = {"total": 0, "counts": {}}
        resp.raise_for_status.return_value = None
        mock_post.return_value = resp
        with patch("os.getcwd", return_value="/app"):
            result = runner.invoke(worktrees, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["total"] == 0

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post", side_effect=httpx.ConnectError("refused"))
    def test_stats_connect_error(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        with patch("os.getcwd", return_value="/app"):
            result = runner.invoke(worktrees, ["stats"])
        assert "Cannot connect to Gobby daemon" in result.output

    @patch("gobby.cli.worktrees.get_daemon_url", return_value="http://localhost:9876")
    @patch("httpx.post")
    def test_stats_http_error(
        self, mock_post: MagicMock, mock_url: MagicMock, runner: CliRunner
    ) -> None:
        resp = MagicMock()
        resp.status_code = 500
        resp.text = "err"
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=resp
        )
        mock_post.return_value = resp
        with patch("os.getcwd", return_value="/app"):
            result = runner.invoke(worktrees, ["stats"])
        assert "HTTP Error 500" in result.output


# =============================================================================
# resolve_worktree_id
# =============================================================================


class TestResolveWorktreeId:
    def test_exact_uuid_match(self) -> None:
        mgr = MagicMock()
        full_id = "a" * 36
        mgr.get.return_value = _make_worktree(id=full_id)
        assert resolve_worktree_id(mgr, full_id) == full_id

    def test_prefix_match_single(self) -> None:
        mgr = MagicMock()
        mgr.get.return_value = None  # not exact
        wt = _make_worktree(id="abc-123-def")
        mgr.list_worktrees.return_value = [wt]
        assert resolve_worktree_id(mgr, "abc") == "abc-123-def"

    def test_prefix_not_found(self) -> None:
        mgr = MagicMock()
        mgr.get.return_value = None
        mgr.list_worktrees.return_value = []
        with pytest.raises(click.ClickException, match="not found"):
            resolve_worktree_id(mgr, "zzz")

    def test_prefix_ambiguous(self) -> None:
        mgr = MagicMock()
        mgr.get.return_value = None
        wt1 = _make_worktree(id="abc-111", branch_name="a")
        wt2 = _make_worktree(id="abc-222", branch_name="b")
        mgr.list_worktrees.return_value = [wt1, wt2]
        with pytest.raises(click.ClickException, match="Ambiguous"):
            resolve_worktree_id(mgr, "abc")
