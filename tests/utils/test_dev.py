"""Tests for dev mode and source-tree detection utilities."""

import os
from pathlib import Path
from unittest.mock import patch

from gobby.utils.dev import (
    WORKTREE_DAEMON_OVERRIDE_ENV,
    LinkedWorktree,
    is_dev_mode,
    is_gobby_project,
    linked_worktree_root,
    running_source_worktree,
    worktree_daemon_refusal,
)


class TestIsGobbyProject:
    """Tests for is_gobby_project()."""

    def test_true_for_gobby_source_repo(self, tmp_path: Path) -> None:
        """Detects the gobby source repo by marker dir + pyproject."""
        (tmp_path / "src" / "gobby" / "install" / "shared").mkdir(parents=True)
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "gobby"\n')
        assert is_gobby_project(tmp_path) is True

    def test_false_without_marker_dir(self, tmp_path: Path) -> None:
        """Returns False if src/gobby/install/shared/ doesn't exist."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "gobby"\n')
        assert is_gobby_project(tmp_path) is False

    def test_false_without_pyproject(self, tmp_path: Path) -> None:
        """Returns False if pyproject.toml is missing."""
        (tmp_path / "src" / "gobby" / "install" / "shared").mkdir(parents=True)
        assert is_gobby_project(tmp_path) is False

    def test_false_for_different_project(self, tmp_path: Path) -> None:
        """Returns False if pyproject.toml is for a different project."""
        (tmp_path / "src" / "gobby" / "install" / "shared").mkdir(parents=True)
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "not-gobby"\n')
        assert is_gobby_project(tmp_path) is False

    def test_single_quote_name(self, tmp_path: Path) -> None:
        """Handles single-quoted project name."""
        (tmp_path / "src" / "gobby" / "install" / "shared").mkdir(parents=True)
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'gobby'\n")
        assert is_gobby_project(tmp_path) is True


class TestIsDevMode:
    """Tests for is_dev_mode()."""

    def test_true_for_gobby_project(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "gobby" / "install" / "shared").mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "gobby"\n')
        assert is_dev_mode(tmp_path) is True

    def test_true_for_gobby_project_subdirectory(self, tmp_path: Path) -> None:
        (tmp_path / "src" / "gobby" / "install" / "shared").mkdir(parents=True)
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "gobby"\n')
        subdirectory = tmp_path / "src" / "gobby" / "cli"
        subdirectory.mkdir(parents=True)

        assert is_dev_mode(subdirectory) is True

    def test_false_for_random_dir(self, tmp_path: Path) -> None:
        assert is_dev_mode(tmp_path) is False

    def test_defaults_to_cwd(self, monkeypatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        assert is_dev_mode() is False


def _make_linked_worktree(tmp_path: Path, *, absolute: bool = True) -> tuple[Path, Path]:
    """Create ``main/.git/worktrees/wt-1`` and a worktree whose ``.git`` file points at it."""
    base = tmp_path.resolve()
    main = base / "main"
    git_dir = main / ".git" / "worktrees" / "wt-1"
    git_dir.mkdir(parents=True)
    worktree = base / "worktrees" / "wt-1"
    worktree.mkdir(parents=True)
    target = git_dir if absolute else Path(os.path.relpath(git_dir, worktree))
    (worktree / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")
    return worktree, main


class TestLinkedWorktreeRoot:
    """Tests for linked_worktree_root()."""

    def test_detects_linked_worktree_from_nested_path(self, tmp_path: Path) -> None:
        worktree, main = _make_linked_worktree(tmp_path)
        nested = worktree / "src" / "gobby"
        nested.mkdir(parents=True)

        assert linked_worktree_root(nested) == LinkedWorktree(root=worktree, main_checkout=main)

    def test_resolves_relative_gitdir_against_the_worktree(self, tmp_path: Path) -> None:
        worktree, main = _make_linked_worktree(tmp_path, absolute=False)

        assert linked_worktree_root(worktree) == LinkedWorktree(root=worktree, main_checkout=main)

    def test_main_working_tree_is_not_linked(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        nested = tmp_path / "src" / "gobby"
        nested.mkdir(parents=True)

        assert linked_worktree_root(nested) is None

    def test_submodule_gitdir_is_not_linked(self, tmp_path: Path) -> None:
        (tmp_path / ".git").write_text("gitdir: /super/.git/modules/sub\n", encoding="utf-8")

        assert linked_worktree_root(tmp_path) is None

    def test_malformed_git_file_is_not_linked(self, tmp_path: Path) -> None:
        (tmp_path / ".git").write_text("not a gitdir pointer\n", encoding="utf-8")

        assert linked_worktree_root(tmp_path) is None

    def test_tree_without_git_is_not_linked(self, tmp_path: Path) -> None:
        nested = tmp_path / "site-packages" / "gobby"
        nested.mkdir(parents=True)

        assert linked_worktree_root(nested) is None


class TestRunningSourceWorktree:
    """Tests for running_source_worktree()."""

    def test_reports_the_package_source_worktree(self, tmp_path: Path) -> None:
        worktree, main = _make_linked_worktree(tmp_path)
        package = worktree / "src" / "gobby"
        package.mkdir(parents=True)

        with patch("gobby.__file__", str(package / "__init__.py")):
            assert running_source_worktree() == LinkedWorktree(root=worktree, main_checkout=main)

    def test_none_for_a_main_checkout_package(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        package = tmp_path / "src" / "gobby"
        package.mkdir(parents=True)

        with patch("gobby.__file__", str(package / "__init__.py")):
            assert running_source_worktree() is None


class TestWorktreeDaemonRefusal:
    """Tests for worktree_daemon_refusal()."""

    def test_names_worktree_main_checkout_and_override(self, tmp_path: Path) -> None:
        worktree, main = _make_linked_worktree(tmp_path)
        package = worktree / "src" / "gobby"
        package.mkdir(parents=True)

        with patch("gobby.__file__", str(package / "__init__.py")):
            refusal = worktree_daemon_refusal(environ={})

        assert refusal is not None
        assert str(worktree) in refusal
        assert str(main) in refusal
        assert f"{WORKTREE_DAEMON_OVERRIDE_ENV}=1" in refusal

    def test_override_env_disarms_the_refusal(self, tmp_path: Path) -> None:
        worktree, _main = _make_linked_worktree(tmp_path)
        package = worktree / "src" / "gobby"
        package.mkdir(parents=True)

        with patch("gobby.__file__", str(package / "__init__.py")):
            assert worktree_daemon_refusal(environ={WORKTREE_DAEMON_OVERRIDE_ENV: "1"}) is None
            assert worktree_daemon_refusal(environ={WORKTREE_DAEMON_OVERRIDE_ENV: "0"}) is not None

    def test_none_outside_a_linked_worktree(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        package = tmp_path / "src" / "gobby"
        package.mkdir(parents=True)

        with patch("gobby.__file__", str(package / "__init__.py")):
            assert worktree_daemon_refusal(environ={}) is None
