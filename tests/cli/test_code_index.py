"""Tests for the code-index CLI module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.code_index.models import IndexResult

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _mock_index_result(**overrides) -> IndexResult:
    defaults = {
        "project_id": "default",
        "files_indexed": 10,
        "files_skipped": 5,
        "symbols_found": 42,
        "duration_ms": 150,
        "errors": [],
    }
    defaults.update(overrides)
    return IndexResult(**defaults)


class TestCodeIndexIndex:
    """Tests for gobby index (default subcommand)."""

    @patch("gobby.cli.code_index._get_indexer")
    def test_index_success(self, mock_get_indexer: MagicMock, runner: CliRunner, tmp_path) -> None:
        mock_indexer = MagicMock()
        mock_indexer.index_directory = AsyncMock(return_value=_mock_index_result())
        mock_get_indexer.return_value = mock_indexer

        result = runner.invoke(cli, ["index", str(tmp_path)])

        assert result.exit_code == 0
        assert "Files indexed: 10" in result.output
        assert "Symbols found: 42" in result.output
        mock_indexer.index_directory.assert_called_once()
        call_kwargs = mock_indexer.index_directory.call_args[1]
        assert call_kwargs["incremental"] is True

    @patch("gobby.cli.code_index._get_indexer")
    def test_index_full_flag(self, mock_get_indexer: MagicMock, runner: CliRunner, tmp_path) -> None:
        mock_indexer = MagicMock()
        mock_indexer.index_directory = AsyncMock(return_value=_mock_index_result(files_skipped=0))
        mock_get_indexer.return_value = mock_indexer

        result = runner.invoke(cli, ["index", str(tmp_path), "--full"])

        assert result.exit_code == 0
        call_kwargs = mock_indexer.index_directory.call_args[1]
        assert call_kwargs["incremental"] is False

    def test_index_invalid_path(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["index", "/nonexistent/path"])

        assert result.exit_code != 0
        assert "Not a directory" in result.output

    @patch("gobby.cli.code_index._get_indexer")
    def test_index_with_errors(self, mock_get_indexer: MagicMock, runner: CliRunner, tmp_path) -> None:
        mock_indexer = MagicMock()
        mock_indexer.index_directory = AsyncMock(return_value=_mock_index_result(
            errors=["parse error in foo.py", "timeout in bar.rs"]
        ))
        mock_get_indexer.return_value = mock_indexer

        result = runner.invoke(cli, ["index", str(tmp_path)])

        assert result.exit_code == 0
        assert "Errors: 2" in result.output
        assert "parse error in foo.py" in result.output

    @patch("gobby.cli.code_index._git_repo_root", return_value="/fake/repo")
    @patch("gobby.cli.code_index._auto_project_id", return_value="auto-proj")
    @patch("gobby.cli.code_index._get_indexer")
    def test_index_default_path_uses_git_root(
        self, mock_get_indexer: MagicMock, mock_pid: MagicMock, mock_git: MagicMock, runner: CliRunner
    ) -> None:
        mock_indexer = MagicMock()
        mock_indexer.index_directory = AsyncMock(return_value=_mock_index_result())
        mock_get_indexer.return_value = mock_indexer

        with patch("gobby.cli.code_index.os.path.isdir", return_value=True):
            result = runner.invoke(cli, ["index"])

        assert result.exit_code == 0
        call_kwargs = mock_indexer.index_directory.call_args[1]
        assert call_kwargs["root_path"] == "/fake/repo"
        assert call_kwargs["project_id"] == "auto-proj"

    @patch("gobby.cli.code_index._get_indexer")
    def test_index_explicit_project_id(self, mock_get_indexer: MagicMock, runner: CliRunner, tmp_path) -> None:
        mock_indexer = MagicMock()
        mock_indexer.index_directory = AsyncMock(return_value=_mock_index_result())
        mock_get_indexer.return_value = mock_indexer

        result = runner.invoke(cli, ["index", str(tmp_path), "-p", "my-proj"])

        assert result.exit_code == 0
        call_kwargs = mock_indexer.index_directory.call_args[1]
        assert call_kwargs["project_id"] == "my-proj"


class TestCodeIndexStatus:
    """Tests for gobby index status."""

    @patch("gobby.cli.code_index._get_storage")
    def test_status_list_projects(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        mock_storage = MagicMock()
        mock_proj = MagicMock()
        mock_proj.id = "proj-1"
        mock_proj.total_files = 50
        mock_proj.total_symbols = 200
        mock_proj.last_indexed_at = ""
        mock_storage.list_indexed_projects.return_value = [mock_proj]
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["index", "status"])

        assert result.exit_code == 0
        assert "proj-1" in result.output
        assert "50 files" in result.output

    @patch("gobby.cli.code_index._get_storage")
    def test_status_no_projects(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        mock_storage = MagicMock()
        mock_storage.list_indexed_projects.return_value = []
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["index", "status"])

        assert result.exit_code == 0
        assert "No indexed projects" in result.output

    @patch("gobby.cli.code_index._get_storage")
    def test_status_specific_project(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        mock_storage = MagicMock()
        mock_stats = MagicMock()
        mock_stats.id = "my-proj"
        mock_stats.root_path = "/home/user/project"
        mock_stats.total_files = 100
        mock_stats.total_symbols = 500
        mock_stats.last_indexed_at = "2025-01-01T00:00:00"
        mock_stats.index_duration_ms = 1200
        mock_storage.get_project_stats.return_value = mock_stats
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["index", "status", "-p", "my-proj"])

        assert result.exit_code == 0
        assert "my-proj" in result.output
        assert "Files: 100" in result.output
        assert "Symbols: 500" in result.output

    @patch("gobby.cli.code_index._get_storage")
    def test_status_project_not_indexed(self, mock_get_storage: MagicMock, runner: CliRunner) -> None:
        mock_storage = MagicMock()
        mock_storage.get_project_stats.return_value = None
        mock_get_storage.return_value = mock_storage

        result = runner.invoke(cli, ["index", "status", "-p", "missing"])

        assert result.exit_code == 0
        assert "not indexed" in result.output


class TestCodeIndexInvalidate:
    """Tests for gobby index invalidate."""

    @patch("gobby.cli.code_index._auto_project_id", return_value="auto-proj")
    @patch("gobby.cli.code_index._get_indexer")
    def test_invalidate_success(self, mock_get_indexer: MagicMock, mock_pid: MagicMock, runner: CliRunner) -> None:
        mock_indexer = MagicMock()
        mock_indexer.invalidate = AsyncMock()
        mock_get_indexer.return_value = mock_indexer

        result = runner.invoke(cli, ["index", "invalidate"])

        assert result.exit_code == 0
        assert "Index invalidated" in result.output
        assert "auto-proj" in result.output
        mock_indexer.invalidate.assert_called_once()

    @patch("gobby.cli.code_index._get_indexer")
    def test_invalidate_explicit_project(self, mock_get_indexer: MagicMock, runner: CliRunner) -> None:
        mock_indexer = MagicMock()
        mock_indexer.invalidate = AsyncMock()
        mock_get_indexer.return_value = mock_indexer

        result = runner.invoke(cli, ["index", "invalidate", "-p", "my-proj"])

        assert result.exit_code == 0
        assert "Index invalidated" in result.output
        assert "my-proj" in result.output


def test_help(runner: CliRunner) -> None:
    """index --help works and shows examples."""
    result = runner.invoke(cli, ["index", "--help"])
    assert result.exit_code == 0
    assert "Code indexing commands" in result.output
    assert "status" in result.output
    assert "invalidate" in result.output


@patch("gobby.cli.code_index._get_indexer")
def test_default_routing_with_path(mock_get_indexer: MagicMock, runner: CliRunner, tmp_path) -> None:
    """gobby index <path> routes to the index subcommand without explicit 'index'."""
    mock_indexer = MagicMock()
    mock_indexer.index_directory = AsyncMock(return_value=_mock_index_result())
    mock_get_indexer.return_value = mock_indexer

    result = runner.invoke(cli, ["index", str(tmp_path)])

    assert result.exit_code == 0
    assert "Files indexed: 10" in result.output
