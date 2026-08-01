"""Tests for ``gobby init`` Git hook/wiki setup behavior."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.utils.project_init import InitResult

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_config() -> MagicMock:
    config = MagicMock()
    config.logging.dir = "/tmp/logs"
    return config


def _git_init(path: Path) -> None:
    subprocess.run(
        ["git", "init"],
        cwd=path,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _init_result(
    project_path: Path, *, project_name: str = "repo", already_existed: bool = False
) -> InitResult:
    return InitResult(
        project_id="proj-wiki-setup",
        project_name=project_name,
        project_path=str(project_path),
        created_at="2024-01-15T10:00:00Z",
        already_existed=already_existed,
        verification=None,
    )


@patch("gobby.cli.init.install_git_hooks")
@patch("gobby.cli.init.resolve_native_bin", return_value=None)
@patch("gobby.cli.init.initialize_project")
@patch("gobby.cli.load_full_config_from_db")
def test_init_installs_hooks_for_git_root(
    mock_load_config: MagicMock,
    mock_initialize: MagicMock,
    _resolve_native_bin: MagicMock,
    mock_install_hooks: MagicMock,
    runner: CliRunner,
    mock_config: MagicMock,
    temp_dir: Path,
) -> None:
    mock_load_config.return_value = mock_config
    target_dir = temp_dir / "repo"
    target_dir.mkdir()
    _git_init(target_dir)
    mock_initialize.return_value = _init_result(target_dir)
    mock_install_hooks.return_value = {
        "success": True,
        "installed": ["pre-push"],
        "skipped": [],
        "wiki_setup": {
            "success": True,
            "gitignore_updated": True,
            "worktree_path": str(temp_dir / "repo-wiki"),
            "branch": "wiki",
            "warnings": [],
            "tracked_files": [],
        },
    }

    result = runner.invoke(cli, ["init", "-C", str(target_dir)])

    assert result.exit_code == 0
    mock_install_hooks.assert_called_once_with(target_dir.resolve())
    assert "Git hooks installed: pre-push" in result.output
    assert "Wiki branch setup:" in result.output


@patch("gobby.cli.init.install_git_hooks")
@patch("gobby.cli.init.resolve_native_bin", return_value=None)
@patch("gobby.cli.init.initialize_project")
@patch("gobby.cli.load_full_config_from_db")
def test_init_skips_hooks_for_monorepo_subdirectory(
    mock_load_config: MagicMock,
    mock_initialize: MagicMock,
    _resolve_native_bin: MagicMock,
    mock_install_hooks: MagicMock,
    runner: CliRunner,
    mock_config: MagicMock,
    temp_dir: Path,
) -> None:
    mock_load_config.return_value = mock_config
    repo_root = temp_dir / "repo"
    subdir = repo_root / "packages" / "app"
    subdir.mkdir(parents=True)
    _git_init(repo_root)
    mock_initialize.return_value = _init_result(subdir, project_name="app")

    result = runner.invoke(cli, ["init", "-C", str(subdir)])

    assert result.exit_code == 0
    mock_install_hooks.assert_not_called()
    assert "Git hooks/wiki setup skipped" in result.output
    assert str(repo_root.resolve()) in result.output


@patch("gobby.cli.init.install_git_hooks")
@patch("gobby.cli.init.resolve_native_bin", return_value=None)
@patch("gobby.cli.init.initialize_project")
@patch("gobby.cli.load_full_config_from_db")
def test_init_skips_hooks_for_non_git_directory(
    mock_load_config: MagicMock,
    mock_initialize: MagicMock,
    _resolve_native_bin: MagicMock,
    mock_install_hooks: MagicMock,
    runner: CliRunner,
    mock_config: MagicMock,
    temp_dir: Path,
) -> None:
    mock_load_config.return_value = mock_config
    target_dir = temp_dir / "plain"
    target_dir.mkdir()
    mock_initialize.return_value = _init_result(target_dir, project_name="plain")

    result = runner.invoke(cli, ["init", "-C", str(target_dir)])

    assert result.exit_code == 0
    mock_install_hooks.assert_not_called()


@patch("gobby.cli.init._maybe_run_linear_setup")
@patch("gobby.cli.init._maybe_install_git_hooks_for_init")
@patch("gobby.cli.init.subprocess.run")
@patch("gobby.cli.init.resolve_native_bin", return_value="/usr/local/bin/gcode")
@patch("gobby.cli.init.initialize_project")
@patch("gobby.cli.load_full_config_from_db")
def test_init_existing_project_runs_initial_index(
    mock_load_config: MagicMock,
    mock_initialize: MagicMock,
    _resolve_native_bin: MagicMock,
    mock_run: MagicMock,
    _install_hooks: MagicMock,
    _linear_setup: MagicMock,
    runner: CliRunner,
    mock_config: MagicMock,
    temp_dir: Path,
) -> None:
    mock_load_config.return_value = mock_config
    target_dir = temp_dir / "repo"
    target_dir.mkdir()
    mock_initialize.return_value = _init_result(target_dir, already_existed=True)
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="indexed\n", stderr=""
    )

    result = runner.invoke(cli, ["init", "-C", str(target_dir)])

    assert result.exit_code == 0
    mock_run.assert_called_once_with(
        ["/usr/local/bin/gcode", "index", "--project", str(target_dir)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert "Project already initialized: repo" in result.output
    assert "Indexing codebase..." in result.output
    assert "indexed" in result.output
    assert "Git hooks/wiki setup skipped" not in result.output
