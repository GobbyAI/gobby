from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.worktrees import (
    _copy_project_json_to_worktree,
    _generate_worktree_path,
    _get_worktree_base_dir,
    _install_provider_hooks,
    _resolve_project_context,
)

pytestmark = pytest.mark.unit


class TestGetWorktreeBaseDir:
    """Tests for _get_worktree_base_dir helper."""

    def test_unix_path(self, tmp_path) -> None:
        """Test path uses ~/.gobby/worktrees."""
        with patch("gobby.mcp_proxy.tools.worktrees._helpers.Path.home", return_value=tmp_path):
            path = _get_worktree_base_dir()
            assert str(path) == str(tmp_path / ".gobby" / "worktrees")
            assert path.exists()

    def test_creates_directory(self, tmp_path) -> None:
        """Test that the directory is created if it doesn't exist."""
        mock_home = tmp_path / "fakehome"
        mock_home.mkdir()
        with patch("gobby.mcp_proxy.tools.worktrees._helpers.Path.home", return_value=mock_home):
            path = _get_worktree_base_dir()
            assert str(path) == str(mock_home / ".gobby" / "worktrees")
            assert path.exists()


class TestGenerateWorktreePath:
    """Tests for _generate_worktree_path helper."""

    def test_with_project_name(self, tmp_path) -> None:
        """Test path generation with project name."""
        with patch(
            "gobby.mcp_proxy.tools.worktrees._helpers.get_worktree_base_dir", return_value=tmp_path
        ):
            path = _generate_worktree_path("feature/test", project_name="myproject")
            assert "myproject" in path
            assert "feature-test" in path

    def test_without_project_name(self, tmp_path) -> None:
        """Test path generation without project name."""
        with patch(
            "gobby.mcp_proxy.tools.worktrees._helpers.get_worktree_base_dir", return_value=tmp_path
        ):
            path = _generate_worktree_path("feature/test")
            assert path == str(tmp_path / "feature-test")


class TestResolveProjectContext:
    """Tests for _resolve_project_context helper."""

    def test_project_path_not_exists(self) -> None:
        """Test with non-existent project path."""
        git_manager, project_id, error = _resolve_project_context(
            project_path="/nonexistent/path",
            default_git_manager=None,
            default_project_id=None,
        )
        assert error is not None
        assert "does not exist" in error
        assert git_manager is None
        assert project_id is None

    def test_project_path_no_gobby(self, tmp_path) -> None:
        """Test with path that has no .gobby/project.json."""
        with patch(
            "gobby.mcp_proxy.tools.worktrees._helpers.get_project_context", return_value=None
        ):
            git_manager, project_id, error = _resolve_project_context(
                project_path=str(tmp_path),
                default_git_manager=None,
                default_project_id=None,
            )
            assert error is not None
            assert "No .gobby/project.json" in error

    def test_project_path_invalid_git_repo(self, tmp_path) -> None:
        """Test with path that's not a valid git repo."""
        with (
            patch(
                "gobby.mcp_proxy.tools.worktrees._helpers.get_project_context",
                return_value={"id": "proj-1", "project_path": str(tmp_path)},
            ),
            patch(
                "gobby.mcp_proxy.tools.worktrees._helpers.WorktreeGitManager",
                side_effect=ValueError("Not a git repo"),
            ),
        ):
            git_manager, project_id, error = _resolve_project_context(
                project_path=str(tmp_path),
                default_git_manager=None,
                default_project_id=None,
            )
            assert error is not None
            assert "Invalid git repository" in error

    def test_no_project_path_no_defaults(self) -> None:
        """Test with no project path and no defaults."""
        with patch(
            "gobby.mcp_proxy.tools.worktrees._helpers.get_project_context", return_value=None
        ):
            git_manager, project_id, error = _resolve_project_context(
                project_path=None,
                default_git_manager=None,
                default_project_id=None,
            )
        assert error is not None
        assert "No project_path provided" in error

    def test_no_project_path_no_project_id(self) -> None:
        """Test with no project path and no project ID default."""
        with patch(
            "gobby.mcp_proxy.tools.worktrees._helpers.get_project_context", return_value=None
        ):
            git_manager, project_id, error = _resolve_project_context(
                project_path=None,
                default_git_manager=MagicMock(),
                default_project_id=None,
            )
        assert error is not None
        assert "No project_path provided" in error

    def test_with_defaults(self) -> None:
        """Test with valid defaults."""
        mock_manager = MagicMock()
        git_manager, project_id, error = _resolve_project_context(
            project_path=None,
            default_git_manager=mock_manager,
            default_project_id="proj-123",
        )
        assert error is None
        assert git_manager is mock_manager
        assert project_id == "proj-123"


class TestCopyProjectJsonToWorktree:
    """Tests for _copy_project_json_to_worktree helper."""

    def test_copies_project_json(self, tmp_path) -> None:
        """Test that project.json is copied with parent reference."""
        repo_path = tmp_path / "repo"
        repo_gobby = repo_path / ".gobby"
        repo_gobby.mkdir(parents=True)
        (repo_gobby / "project.json").write_text('{"id": "proj-1", "name": "test"}')

        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        _copy_project_json_to_worktree(repo_path, worktree_path)

        worktree_project = worktree_path / ".gobby" / "project.json"
        assert worktree_project.exists()
        import json

        data = json.loads(worktree_project.read_text())
        assert data["id"] == "proj-1"
        assert "parent_project_path" in data

    def test_skips_if_no_source(self, tmp_path) -> None:
        """Test that nothing happens if source doesn't exist."""
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        _copy_project_json_to_worktree(repo_path, worktree_path)

        assert not (worktree_path / ".gobby" / "project.json").exists()

    def test_augments_existing_with_parent_path(self, tmp_path) -> None:
        """Test that existing project.json is overwritten with parent_project_path."""
        repo_path = tmp_path / "repo"
        repo_gobby = repo_path / ".gobby"
        repo_gobby.mkdir(parents=True)
        (repo_gobby / "project.json").write_text('{"id": "proj-1"}')

        worktree_path = tmp_path / "worktree"
        worktree_gobby = worktree_path / ".gobby"
        worktree_gobby.mkdir(parents=True)
        (worktree_gobby / "project.json").write_text('{"id": "proj-1"}')

        _copy_project_json_to_worktree(repo_path, worktree_path)

        import json

        data = json.loads((worktree_gobby / "project.json").read_text())
        assert data["id"] == "proj-1"
        assert "parent_project_path" in data


class TestInstallProviderHooks:
    """Tests for _install_provider_hooks helper."""

    def test_none_provider(self, tmp_path) -> None:
        """Test with None provider returns False."""
        result = _install_provider_hooks(None, tmp_path)
        assert result is False

    def test_claude_hooks_success(self, tmp_path) -> None:
        """Test Claude hooks installation success with project mode."""
        from gobby.cli.installers import claude as claude_mod

        with patch.object(claude_mod, "install_claude") as mock_install:
            mock_install.return_value = {"success": True}
            result = _install_provider_hooks("claude", tmp_path)
            assert result is True
            mock_install.assert_called_once_with(tmp_path, mode="project")

    def test_claude_hooks_failure(self, tmp_path, caplog) -> None:
        """Test Claude hooks installation failure."""
        from gobby.cli.installers import claude as claude_mod

        with patch.object(claude_mod, "install_claude") as mock_install:
            mock_install.return_value = {"success": False, "error": "Install failed"}
            result = _install_provider_hooks("claude", tmp_path)
            assert result is False
            mock_install.assert_called_once_with(tmp_path, mode="project")
            assert "Install failed" in caplog.text

    def test_gemini_hooks_success(self, tmp_path) -> None:
        """Test Gemini hooks installation success."""
        from gobby.cli.installers import gemini as gemini_mod

        with patch.object(gemini_mod, "install_gemini") as mock_install:
            mock_install.return_value = {"success": True}
            result = _install_provider_hooks("gemini", tmp_path)
            assert result is True

    def test_gemini_hooks_failure(self, tmp_path, caplog) -> None:
        """Test Gemini hooks installation failure."""
        from gobby.cli.installers import gemini as gemini_mod

        with patch.object(gemini_mod, "install_gemini") as mock_install:
            mock_install.return_value = {"success": False, "error": "Failed"}
            result = _install_provider_hooks("gemini", tmp_path)
            assert result is False
            assert "Failed" in caplog.text

    def test_qwen_hooks_success(self, tmp_path) -> None:
        """Test Qwen hooks installation success with project mode."""
        from gobby.cli.installers import qwen as qwen_mod

        with patch.object(qwen_mod, "install_qwen") as mock_install:
            mock_install.return_value = {"success": True}
            result = _install_provider_hooks("qwen", tmp_path)
            assert result is True
            mock_install.assert_called_once_with(tmp_path, mode="project")

    def test_droid_hooks_success(self, tmp_path) -> None:
        """Test Droid hooks installation success with project mode."""
        from gobby.cli.installers import droid as droid_mod

        with patch.object(droid_mod, "install_droid") as mock_install:
            mock_install.return_value = {"success": True}
            result = _install_provider_hooks("droid", tmp_path)
            assert result is True
            mock_install.assert_called_once_with(tmp_path, mode="project")

    def test_hooks_install_exception(self, tmp_path, caplog) -> None:
        """Test hooks installation handles exceptions."""
        from gobby.cli.installers import claude as claude_mod

        with patch.object(claude_mod, "install_claude") as mock_install:
            mock_install.side_effect = Exception("Import error")
            result = _install_provider_hooks("claude", tmp_path)
            assert result is False
            assert "Import error" in caplog.text
