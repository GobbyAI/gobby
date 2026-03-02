from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.worktrees import (
    _copy_project_json_to_worktree,
    _generate_worktree_path,
    _get_worktree_base_dir,
    _install_provider_hooks,
    _resolve_project_context,
    create_worktrees_registry,
)
from gobby.storage.worktrees import Worktree

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_worktree_storage():
    return MagicMock()


@pytest.fixture
def mock_git_manager():
    manager = MagicMock()
    manager.repo_path = "/tmp/repo"
    return manager


@pytest.fixture
def registry(mock_worktree_storage, mock_git_manager):
    return create_worktrees_registry(
        worktree_storage=mock_worktree_storage,
        git_manager=mock_git_manager,
        project_id="proj-1",
    )


@pytest.mark.asyncio
async def test_create_worktree_success(registry, mock_worktree_storage, mock_git_manager):
    mock_git_manager.create_worktree.return_value.success = True
    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.return_value = Worktree(
        id="wt-123",
        project_id="proj-1",
        task_id=None,
        branch_name="feature/test",
        worktree_path="/tmp/wt/feature-test",
        base_branch="main",
        agent_session_id=None,
        status="active",
        created_at="now",
        updated_at="now",
        merged_at=None,
    )
    result = await registry.call(
        "create_worktree", {"branch_name": "feature/test", "worktree_path": "/tmp/wt/feature-test"}
    )
    assert result["success"] is True
    assert result["worktree_path"] == "/tmp/wt/feature-test"
    mock_git_manager.create_worktree.assert_called_once_with(
        worktree_path="/tmp/wt/feature-test",
        branch_name="feature/test",
        base_branch="main",
        create_branch=True,
    )
    mock_worktree_storage.create.assert_called_once()


@pytest.mark.asyncio
async def test_create_worktree_failure(registry, mock_worktree_storage, mock_git_manager):
    mock_git_manager.create_worktree.return_value.success = False
    mock_git_manager.create_worktree.return_value.error = "Git error"
    mock_worktree_storage.get_by_branch.return_value = None
    result = await registry.call(
        "create_worktree",
        {
            "branch_name": "feature/fail",
        },
    )
    assert result["success"] is False
    assert "Git error" in result["error"]
    mock_worktree_storage.create.assert_not_called()


@pytest.mark.asyncio
async def test_create_worktree_existing(registry, mock_worktree_storage):
    existing = Worktree(
        id="wt-123",
        project_id="proj-1",
        branch_name="feature/exists",
        worktree_path="/tmp/exists",
        base_branch="main",
        status="active",
        created_at="2024-01-01",
        updated_at="2024-01-01",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get_by_branch.return_value = existing
    result = await registry.call(
        "create_worktree",
        {
            "branch_name": "feature/exists",
        },
    )
    assert result["success"] is False
    assert "already exists" in result["error"]


@pytest.mark.asyncio
async def test_create_worktree_auto_path(registry, mock_git_manager, mock_worktree_storage):
    mock_git_manager.create_worktree.return_value.success = True
    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.return_value = Worktree(
        id="wt-auto",
        project_id="proj-1",
        task_id=None,
        branch_name="feature/auto",
        worktree_path="/tmp/gobby-worktrees/feature-auto",
        base_branch="main",
        agent_session_id=None,
        status="active",
        created_at="now",
        updated_at="now",
        merged_at=None,
    )
    with patch(
        "gobby.mcp_proxy.tools.worktrees._get_worktree_base_dir",
        return_value=Path("/tmp/gobby-worktrees"),
    ):
        result = await registry.call(
            "create_worktree",
            {
                "branch_name": "feature/auto",
            },
        )
        assert result["success"] is True
        args, kwargs = mock_git_manager.create_worktree.call_args
        assert "feature-auto" in kwargs["worktree_path"]


@pytest.mark.asyncio
async def test_get_worktree_found(registry, mock_worktree_storage, mock_git_manager):
    wt = Worktree(
        id="wt-123",
        project_id="proj-1",
        branch_name="feat/1",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_status = MagicMock()
    mock_status.has_uncommitted_changes = True
    mock_status.ahead = 1
    mock_status.behind = 2
    mock_status.branch = "feat/1"
    mock_git_manager.get_worktree_status.return_value = mock_status
    with patch("pathlib.Path.exists", return_value=True):
        result = await registry.call("get_worktree", {"worktree_id": "wt-123"})
        assert result["success"] is True
        assert result["worktree"]["id"] == "wt-123"
        assert result["git_status"]["has_uncommitted_changes"] is True


@pytest.mark.asyncio
async def test_get_worktree_not_found(registry, mock_worktree_storage):
    mock_worktree_storage.get.return_value = None
    result = await registry.call("get_worktree", {"worktree_id": "missing"})
    assert result["success"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_list_worktrees(registry, mock_worktree_storage):
    wt1 = Worktree(
        id="1",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.list_worktrees.return_value = [wt1]
    result = await registry.call("list_worktrees", {"status": "active"})
    assert result["success"] is True
    assert len(result["worktrees"]) == 1
    mock_worktree_storage.list_worktrees.assert_called_with(
        project_id="proj-1", status="active", agent_session_id=None, limit=50
    )


@pytest.mark.asyncio
async def test_claim_worktree_success(registry, mock_worktree_storage):
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        agent_session_id=None,
        task_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_worktree_storage.claim.return_value = True
    result = await registry.call("claim_worktree", {"worktree_id": "wt-1", "session_id": "sess-1"})
    assert result["success"] is True
    mock_worktree_storage.claim.assert_called_with("wt-1", "sess-1")


@pytest.mark.asyncio
async def test_claim_worktree_already_claimed(registry, mock_worktree_storage):
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        agent_session_id="other-session",
        task_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    result = await registry.call("claim_worktree", {"worktree_id": "wt-1", "session_id": "sess-1"})
    assert result["success"] is False
    assert "already claimed" in result["error"]


@pytest.mark.asyncio
async def test_release_worktree(registry, mock_worktree_storage):
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        agent_session_id="sess-1",
        task_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_worktree_storage.release.return_value = True
    result = await registry.call("release_worktree", {"worktree_id": "wt-1"})
    assert result["success"] is True
    mock_worktree_storage.release.assert_called_with("wt-1")


@pytest.mark.asyncio
async def test_delete_worktree_success(registry, mock_worktree_storage, mock_git_manager):
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.get_worktree_status.return_value.has_uncommitted_changes = False
    mock_git_manager.delete_worktree.return_value.success = True
    mock_worktree_storage.delete.return_value = True
    with patch("pathlib.Path.exists", return_value=True):
        result = await registry.call("delete_worktree", {"worktree_id": "wt-1"})
        assert result["success"] is True
        mock_git_manager.delete_worktree.assert_called_with(
            "/tmp/p1", force=False, delete_branch=True, branch_name="b1"
        )
        mock_worktree_storage.delete.assert_called_with("wt-1")


@pytest.mark.asyncio
async def test_delete_worktree_uncommitted_changes(
    registry, mock_worktree_storage, mock_git_manager
):
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.get_worktree_status.return_value.has_uncommitted_changes = True
    with patch("pathlib.Path.exists", return_value=True):
        result = await registry.call("delete_worktree", {"worktree_id": "wt-1"})
        assert result["success"] is False
        assert "uncommitted changes" in result["error"]

        mock_git_manager.delete_worktree.return_value.success = True
        mock_worktree_storage.delete.return_value = True
        result_force = await registry.call(
            "delete_worktree", {"worktree_id": "wt-1", "force": True}
        )
        assert result_force["success"] is True
        mock_git_manager.delete_worktree.assert_called_with(
            "/tmp/p1", force=True, delete_branch=True, branch_name="b1"
        )


@pytest.mark.asyncio
async def test_sync_worktree(registry, mock_worktree_storage, mock_git_manager):
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.sync_from_main.return_value.success = True
    mock_git_manager.sync_from_main.return_value.message = "Synced"
    result = await registry.call("sync_worktree", {"worktree_id": "wt-1", "strategy": "merge"})
    assert result["success"] is True
    mock_git_manager.sync_from_main.assert_called_with(
        "/tmp/p1", base_branch="main", strategy="merge"
    )


@pytest.mark.asyncio
async def test_detect_stale_worktrees(registry, mock_worktree_storage):
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="p1",
        base_branch="main",
        status="active",
        created_at="old",
        updated_at="old",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.find_stale.return_value = [wt]
    result = await registry.call("detect_stale_worktrees", {"hours": 48})
    assert result["success"] is True
    assert result["count"] == 1
    mock_worktree_storage.find_stale.assert_called_with(project_id="proj-1", hours=48, limit=50)


@pytest.mark.asyncio
async def test_cleanup_stale_worktrees(registry, mock_worktree_storage, mock_git_manager):
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at="old",
        updated_at="old",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.cleanup_stale.return_value = [wt]
    mock_git_manager.delete_worktree.return_value.success = True

    # Dry run
    result = await registry.call("cleanup_stale_worktrees", {"hours": 24, "dry_run": True})
    assert result["success"] is True
    assert result["count"] == 1
    assert result["cleaned"][0]["marked_abandoned"] is False
    mock_worktree_storage.cleanup_stale.assert_called_with(
        project_id="proj-1", hours=24, dry_run=True
    )

    # Actual run with delete_git
    result = await registry.call(
        "cleanup_stale_worktrees", {"hours": 24, "dry_run": False, "delete_git": True}
    )
    assert result["success"] is True
    assert result["cleaned"][0]["marked_abandoned"] is True
    assert result["cleaned"][0]["git_deleted"] is True
    mock_git_manager.delete_worktree.assert_called_with(
        "/tmp/p1", force=True, delete_branch=True, branch_name="b1"
    )


# ========== Helper function tests ==========


class TestGetWorktreeBaseDir:
    """Tests for _get_worktree_base_dir helper."""

    def test_unix_path(self) -> None:
        """Test Unix path generation."""
        with patch("gobby.mcp_proxy.tools.worktrees.platform.system", return_value="Darwin"):
            path = _get_worktree_base_dir()
            assert "gobby-worktrees" in str(path)
            # On macOS, /tmp resolves to /private/tmp
            assert path.exists() or str(path).startswith("/")

    def test_windows_path(self, tmp_path) -> None:
        """Test Windows path generation uses tempdir."""
        with (
            patch("gobby.mcp_proxy.tools.worktrees.platform.system", return_value="Windows"),
            patch(
                "gobby.mcp_proxy.tools.worktrees.tempfile.gettempdir", return_value=str(tmp_path)
            ),
        ):
            path = _get_worktree_base_dir()
            assert "gobby-worktrees" in str(path)
            assert str(tmp_path) in str(path)


class TestGenerateWorktreePath:
    """Tests for _generate_worktree_path helper."""

    def test_with_project_name(self, tmp_path) -> None:
        """Test path generation with project name."""
        with patch("gobby.mcp_proxy.tools.worktrees._get_worktree_base_dir", return_value=tmp_path):
            path = _generate_worktree_path("feature/test", project_name="myproject")
            assert "myproject" in path
            assert "feature-test" in path

    def test_without_project_name(self, tmp_path) -> None:
        """Test path generation without project name."""
        with patch("gobby.mcp_proxy.tools.worktrees._get_worktree_base_dir", return_value=tmp_path):
            path = _generate_worktree_path("feature/test")
            # No project subdirectory
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
        with patch("gobby.mcp_proxy.tools.worktrees.get_project_context", return_value=None):
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
                "gobby.mcp_proxy.tools.worktrees.get_project_context",
                return_value={"id": "proj-1", "project_path": str(tmp_path)},
            ),
            patch(
                "gobby.mcp_proxy.tools.worktrees.WorktreeGitManager",
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
        from unittest.mock import patch

        with patch("gobby.mcp_proxy.tools.worktrees.get_project_context", return_value=None):
            git_manager, project_id, error = _resolve_project_context(
                project_path=None,
                default_git_manager=None,
                default_project_id=None,
            )
        assert error is not None
        assert "No project_path provided" in error

    def test_no_project_path_no_project_id(self) -> None:
        """Test with no project path and no project ID default."""
        from unittest.mock import patch

        with patch("gobby.mcp_proxy.tools.worktrees.get_project_context", return_value=None):
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
        # Setup source
        repo_path = tmp_path / "repo"
        repo_gobby = repo_path / ".gobby"
        repo_gobby.mkdir(parents=True)
        (repo_gobby / "project.json").write_text('{"id": "proj-1", "name": "test"}')

        # Setup target
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir()

        _copy_project_json_to_worktree(repo_path, worktree_path)

        # Verify copy exists with parent reference
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

        # Should not raise
        _copy_project_json_to_worktree(repo_path, worktree_path)

        assert not (worktree_path / ".gobby" / "project.json").exists()

    def test_skips_if_target_exists(self, tmp_path) -> None:
        """Test that existing project.json is not overwritten."""
        # Setup source
        repo_path = tmp_path / "repo"
        repo_gobby = repo_path / ".gobby"
        repo_gobby.mkdir(parents=True)
        (repo_gobby / "project.json").write_text('{"id": "proj-1"}')

        # Setup target with existing file
        worktree_path = tmp_path / "worktree"
        worktree_gobby = worktree_path / ".gobby"
        worktree_gobby.mkdir(parents=True)
        (worktree_gobby / "project.json").write_text('{"id": "existing"}')

        _copy_project_json_to_worktree(repo_path, worktree_path)

        # Verify original is preserved
        import json

        data = json.loads((worktree_gobby / "project.json").read_text())
        assert data["id"] == "existing"


class TestInstallProviderHooks:
    """Tests for _install_provider_hooks helper."""

    def test_none_provider(self, tmp_path) -> None:
        """Test with None provider returns False."""
        result = _install_provider_hooks(None, tmp_path)
        assert result is False

    def test_claude_hooks_success(self, tmp_path) -> None:
        """Test Claude hooks installation success."""
        from gobby.cli.installers import claude as claude_mod

        with patch.object(claude_mod, "install_claude") as mock_install:
            mock_install.return_value = {"success": True}
            result = _install_provider_hooks("claude", tmp_path)
            assert result is True
            mock_install.assert_called_once()

    def test_claude_hooks_failure(self, tmp_path, caplog) -> None:
        """Test Claude hooks installation failure."""
        from gobby.cli.installers import claude as claude_mod

        with patch.object(claude_mod, "install_claude") as mock_install:
            mock_install.return_value = {"success": False, "error": "Install failed"}
            result = _install_provider_hooks("claude", tmp_path)
            assert result is False
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

    def test_antigravity_hooks_success(self, tmp_path) -> None:
        """Test Antigravity hooks installation success."""
        from gobby.cli.installers import antigravity as antigravity_mod

        with patch.object(antigravity_mod, "install_antigravity") as mock_install:
            mock_install.return_value = {"success": True}
            result = _install_provider_hooks("antigravity", tmp_path)
            assert result is True

    def test_antigravity_hooks_failure(self, tmp_path, caplog) -> None:
        """Test Antigravity hooks installation failure."""
        from gobby.cli.installers import antigravity as antigravity_mod

        with patch.object(antigravity_mod, "install_antigravity") as mock_install:
            mock_install.return_value = {"success": False, "error": "Install failed"}
            result = _install_provider_hooks("antigravity", tmp_path)
            assert result is False
            assert "Install failed" in caplog.text

    def test_hooks_install_exception(self, tmp_path, caplog) -> None:
        """Test hooks installation handles exceptions."""
        from gobby.cli.installers import claude as claude_mod

        with patch.object(claude_mod, "install_claude") as mock_install:
            mock_install.side_effect = Exception("Import error")
            result = _install_provider_hooks("claude", tmp_path)
            assert result is False
            assert "Import error" in caplog.text


# ========== Additional tool tests for coverage ==========


@pytest.mark.asyncio
async def test_get_worktree_path_not_exists(registry, mock_worktree_storage, mock_git_manager):
    """Test get_worktree when path doesn't exist on disk."""
    wt = Worktree(
        id="wt-123",
        project_id="proj-1",
        branch_name="feat/1",
        worktree_path="/nonexistent/path",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    with patch("pathlib.Path.exists", return_value=False):
        result = await registry.call("get_worktree", {"worktree_id": "wt-123"})
        assert result["success"] is True
        # git_status should be None or missing when path doesn't exist
        assert result.get("git_status") is None or "git_status" not in result


@pytest.mark.asyncio
async def test_claim_worktree_not_found(registry, mock_worktree_storage):
    """Test claim_worktree when worktree not found."""
    mock_worktree_storage.get.return_value = None
    result = await registry.call(
        "claim_worktree", {"worktree_id": "nonexistent", "session_id": "sess-1"}
    )
    assert result["success"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_release_worktree_not_found(registry, mock_worktree_storage):
    """Test release_worktree when worktree not found."""
    mock_worktree_storage.get.return_value = None
    result = await registry.call("release_worktree", {"worktree_id": "nonexistent"})
    # release_worktree returns {"error": "..."} without "success" key on error
    assert "error" in result
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_delete_worktree_not_found(registry, mock_worktree_storage):
    """Test delete_worktree is idempotent when worktree not found."""
    mock_worktree_storage.get.return_value = None
    result = await registry.call("delete_worktree", {"worktree_id": "nonexistent"})
    assert result["success"] is True
    assert result["already_deleted"] is True


@pytest.mark.asyncio
async def test_delete_worktree_path_not_exists(registry, mock_worktree_storage, mock_git_manager):
    """Test delete_worktree when path doesn't exist (orphaned DB record)."""
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="/nonexistent",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_worktree_storage.delete.return_value = True
    with patch("pathlib.Path.exists", return_value=False):
        result = await registry.call("delete_worktree", {"worktree_id": "wt-1"})
        # Should succeed - just cleans up DB record when path doesn't exist
        assert result["success"] is True
        # Git delete is NOT called when path doesn't exist (orphaned DB record cleanup)
        mock_git_manager.delete_worktree.assert_not_called()
        # But DB record is still deleted
        mock_worktree_storage.delete.assert_called_once_with("wt-1")


@pytest.mark.asyncio
async def test_delete_worktree_git_failure(registry, mock_worktree_storage, mock_git_manager):
    """Test delete_worktree when git delete fails."""
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.get_worktree_status.return_value.has_uncommitted_changes = False
    mock_git_manager.delete_worktree.return_value.success = False
    mock_git_manager.delete_worktree.return_value.error = "Git delete failed"
    with patch("pathlib.Path.exists", return_value=True):
        result = await registry.call("delete_worktree", {"worktree_id": "wt-1"})
        assert result["success"] is False
        assert "Git delete failed" in result["error"]


@pytest.mark.asyncio
async def test_sync_worktree_not_found(registry, mock_worktree_storage):
    """Test sync_worktree when worktree not found."""
    mock_worktree_storage.get.return_value = None
    result = await registry.call("sync_worktree", {"worktree_id": "nonexistent"})
    assert result["success"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_sync_worktree_failure(registry, mock_worktree_storage, mock_git_manager):
    """Test sync_worktree when sync fails."""
    wt = Worktree(
        id="wt-1",
        project_id="p1",
        branch_name="b1",
        worktree_path="/tmp/p1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager.sync_from_main.return_value.success = False
    mock_git_manager.sync_from_main.return_value.error = "Sync failed"
    result = await registry.call("sync_worktree", {"worktree_id": "wt-1"})
    assert result["success"] is False
    assert "Sync failed" in result["error"]


# ========== merge_worktree tests ==========


@pytest.mark.asyncio
async def test_merge_worktree_success(registry, mock_worktree_storage, mock_git_manager):
    """Merge worktree successfully (fully isolated in worktree)."""
    wt = Worktree(
        id="wt-1",
        project_id="proj-1",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(
        returncode=0, stdout="main", stderr=""
    )
    mock_worktree_storage.mark_merged.return_value = True

    result = await registry.call(
        "merge_worktree", {"worktree_id": "wt-1", "target_branch": "main"}
    )

    assert result["success"] is True
    assert result["source_branch"] == "feature/test"
    assert result["target_branch"] == "main"
    mock_worktree_storage.mark_merged.assert_called_once_with("wt-1")
    # Verify merge happened in the worktree
    calls = mock_git_manager._run_git.call_args_list
    merge_call = [c for c in calls if c[0][0][:1] == ["merge"] and "--no-edit" in c[0][0]]
    assert len(merge_call) == 1
    assert merge_call[0].kwargs.get("cwd") == "/tmp/wt1" or merge_call[0][1].get("cwd") == "/tmp/wt1"
    # Verify push_command is returned for the agent to execute
    assert result["push_command"] == "git push --no-verify origin feature/test:main"
    # Verify NO push was executed by the tool (agent handles push)
    push_calls = [c for c in calls if c[0][0][:1] == ["push"]]
    assert len(push_calls) == 0


@pytest.mark.asyncio
async def test_merge_worktree_not_found(registry, mock_worktree_storage):
    """Merge fails when worktree not found."""
    mock_worktree_storage.get.return_value = None

    result = await registry.call(
        "merge_worktree", {"worktree_id": "missing", "target_branch": "main"}
    )

    assert result["success"] is False
    assert "not found" in result["error"]


@pytest.mark.asyncio
async def test_merge_worktree_default_target_branch(
    registry, mock_worktree_storage, mock_git_manager
):
    """Merge defaults target_branch to worktree's base_branch."""
    wt = Worktree(
        id="wt-1",
        project_id="proj-1",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="develop",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(
        returncode=0, stdout="develop", stderr=""
    )
    mock_worktree_storage.mark_merged.return_value = True

    result = await registry.call("merge_worktree", {"worktree_id": "wt-1"})

    assert result["success"] is True
    # Verify the merge target was "develop" (from base_branch)
    calls = mock_git_manager._run_git.call_args_list
    wt_merge = [c for c in calls if c[0][0][:1] == ["merge"] and "--no-edit" in c[0][0]]
    assert len(wt_merge) == 1
    assert "origin/develop" in wt_merge[0][0][0]


@pytest.mark.asyncio
async def test_merge_worktree_conflict(registry, mock_worktree_storage, mock_git_manager):
    """Merge detects conflicts in worktree and aborts cleanly."""
    wt = Worktree(
        id="wt-1",
        project_id="proj-1",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt

    def _run_git_side_effect(args, cwd=None, timeout=30, check=False):
        if args[0] == "fetch":
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[0] == "merge" and "--no-edit" in args:
            return MagicMock(
                returncode=1,
                stdout="CONFLICT (content): Merge conflict in src/foo.py\nCONFLICT (content): Merge conflict in src/bar.py",
                stderr="Automatic merge failed",
            )
        # merge --abort
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_git_manager._run_git.side_effect = _run_git_side_effect

    result = await registry.call(
        "merge_worktree", {"worktree_id": "wt-1", "target_branch": "main"}
    )

    assert result["success"] is False
    assert result["has_conflicts"] is True
    assert len(result["conflicted_files"]) == 2
    mock_worktree_storage.mark_merged.assert_not_called()
    # Verify merge --abort was called to clean up the worktree
    abort_calls = [
        c for c in mock_git_manager._run_git.call_args_list
        if c[0][0] == ["merge", "--abort"]
    ]
    assert len(abort_calls) == 1


@pytest.mark.asyncio
async def test_merge_worktree_non_conflict_failure(
    registry, mock_worktree_storage, mock_git_manager
):
    """Merge fails with non-conflict error."""
    wt = Worktree(
        id="wt-1",
        project_id="proj-1",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt

    def _run_git_side_effect(args, cwd=None, timeout=30, check=False):
        if args[0] == "fetch":
            return MagicMock(returncode=0, stdout="", stderr="")
        if args[0] == "merge" and "--no-edit" in args:
            return MagicMock(
                returncode=128,
                stdout="",
                stderr="fatal: Not a valid object name 'main'",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    mock_git_manager._run_git.side_effect = _run_git_side_effect

    result = await registry.call(
        "merge_worktree", {"worktree_id": "wt-1", "target_branch": "main"}
    )

    assert result["success"] is False
    assert result["has_conflicts"] is False
    mock_worktree_storage.mark_merged.assert_not_called()


@pytest.mark.asyncio
async def test_merge_worktree_explicit_source_branch(
    registry, mock_worktree_storage, mock_git_manager
):
    """Agent can specify source_branch explicitly."""
    wt = Worktree(
        id="wt-1",
        project_id="proj-1",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(
        returncode=0, stdout="", stderr=""
    )
    mock_worktree_storage.mark_merged.return_value = True

    result = await registry.call(
        "merge_worktree",
        {"worktree_id": "wt-1", "source_branch": "my-branch", "target_branch": "main"},
    )

    assert result["success"] is True
    assert result["source_branch"] == "my-branch"
    # push_command should use the explicit source branch
    assert result["push_command"] == "git push --no-verify origin my-branch:main"
    # No push executed by the tool
    push_calls = [
        c for c in mock_git_manager._run_git.call_args_list
        if c[0][0][:1] == ["push"]
    ]
    assert len(push_calls) == 0


@pytest.mark.asyncio
async def test_merge_worktree_no_main_repo_operations(
    registry, mock_worktree_storage, mock_git_manager
):
    """All git commands run in the worktree, never the main repo."""
    wt = Worktree(
        id="wt-1",
        project_id="proj-1",
        branch_name="feature/test",
        worktree_path="/tmp/wt1",
        base_branch="main",
        status="active",
        created_at="",
        updated_at="",
        task_id=None,
        agent_session_id=None,
        merged_at=None,
    )
    mock_worktree_storage.get.return_value = wt
    mock_git_manager._run_git.return_value = MagicMock(
        returncode=0, stdout="", stderr=""
    )
    mock_worktree_storage.mark_merged.return_value = True

    result = await registry.call(
        "merge_worktree", {"worktree_id": "wt-1", "target_branch": "main"}
    )

    assert result["success"] is True
    # Every _run_git call must have cwd set to the worktree path
    for call in mock_git_manager._run_git.call_args_list:
        cwd = call.kwargs.get("cwd") or (call[1].get("cwd") if len(call) > 1 else None)
        assert cwd == "/tmp/wt1", f"Git command ran without worktree cwd: {call}"
