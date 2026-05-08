from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.worktrees import create_worktrees_registry
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
async def test_create_worktree_success(registry, mock_worktree_storage, mock_git_manager) -> None:
    mock_git_manager.has_unpushed_commits.return_value = (False, 0)
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
        use_local=False,
    )
    mock_worktree_storage.create.assert_called_once()


@pytest.mark.asyncio
async def test_create_worktree_installs_droid_hooks(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    mock_git_manager.has_unpushed_commits.return_value = (False, 0)
    mock_git_manager.create_worktree.return_value.success = True
    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.return_value = Worktree(
        id="wt-droid",
        project_id="proj-1",
        task_id=None,
        branch_name="feature/droid",
        worktree_path="/tmp/wt/feature-droid",
        base_branch="main",
        agent_session_id=None,
        status="active",
        created_at="now",
        updated_at="now",
        merged_at=None,
    )

    with (
        patch("gobby.mcp_proxy.tools.worktrees._create.copy_project_json_to_worktree"),
        patch(
            "gobby.mcp_proxy.tools.worktrees._create.install_provider_hooks",
            return_value=True,
        ) as mock_install,
    ):
        result = await registry.call(
            "create_worktree",
            {
                "branch_name": "feature/droid",
                "worktree_path": "/tmp/wt/feature-droid",
                "provider": "droid",
            },
        )

    assert result["success"] is True
    assert result["hooks_installed"] is True
    mock_install.assert_called_once_with("droid", "/tmp/wt/feature-droid")


@pytest.mark.asyncio
async def test_create_worktree_failure(registry, mock_worktree_storage, mock_git_manager) -> None:
    mock_git_manager.has_unpushed_commits.return_value = (False, 0)
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
async def test_create_worktree_existing(registry, mock_worktree_storage) -> None:
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
async def test_create_worktree_auto_path(registry, mock_git_manager, mock_worktree_storage) -> None:
    mock_git_manager.has_unpushed_commits.return_value = (False, 0)
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
        "gobby.mcp_proxy.tools.worktrees._helpers.get_worktree_base_dir",
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
async def test_create_worktree_use_local_explicit(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Test create_worktree with explicit use_local=True passes through."""
    mock_git_manager.create_worktree.return_value.success = True
    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.return_value = Worktree(
        id="wt-local",
        project_id="proj-1",
        task_id=None,
        branch_name="feature/local",
        worktree_path="/tmp/wt/feature-local",
        base_branch="develop",
        agent_session_id=None,
        status="active",
        created_at="now",
        updated_at="now",
        merged_at=None,
    )
    result = await registry.call(
        "create_worktree",
        {
            "branch_name": "feature/local",
            "base_branch": "develop",
            "worktree_path": "/tmp/wt/feature-local",
            "use_local": True,
        },
    )
    assert result["success"] is True
    mock_git_manager.create_worktree.assert_called_once_with(
        worktree_path="/tmp/wt/feature-local",
        branch_name="feature/local",
        base_branch="develop",
        create_branch=True,
        use_local=True,
    )
    mock_git_manager.has_unpushed_commits.assert_not_called()


@pytest.mark.asyncio
async def test_create_worktree_auto_detects_unpushed(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Test create_worktree auto-sets use_local=True when base_branch has unpushed commits."""
    mock_git_manager.has_unpushed_commits.return_value = (True, 3)
    mock_git_manager.create_worktree.return_value.success = True
    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.return_value = Worktree(
        id="wt-auto-local",
        project_id="proj-1",
        task_id=None,
        branch_name="feature/auto-local",
        worktree_path="/tmp/wt/feature-auto-local",
        base_branch="main",
        agent_session_id=None,
        status="active",
        created_at="now",
        updated_at="now",
        merged_at=None,
    )
    result = await registry.call(
        "create_worktree",
        {
            "branch_name": "feature/auto-local",
            "worktree_path": "/tmp/wt/feature-auto-local",
        },
    )
    assert result["success"] is True
    mock_git_manager.has_unpushed_commits.assert_called_once_with("main")
    mock_git_manager.create_worktree.assert_called_once_with(
        worktree_path="/tmp/wt/feature-auto-local",
        branch_name="feature/auto-local",
        base_branch="main",
        create_branch=True,
        use_local=True,
    )


@pytest.mark.asyncio
async def test_create_worktree_no_unpushed_uses_remote(
    registry, mock_worktree_storage, mock_git_manager
) -> None:
    """Test create_worktree defaults to use_local=False when no unpushed commits."""
    mock_git_manager.has_unpushed_commits.return_value = (False, 0)
    mock_git_manager.create_worktree.return_value.success = True
    mock_worktree_storage.get_by_branch.return_value = None
    mock_worktree_storage.create.return_value = Worktree(
        id="wt-remote",
        project_id="proj-1",
        task_id=None,
        branch_name="feature/remote",
        worktree_path="/tmp/wt/feature-remote",
        base_branch="main",
        agent_session_id=None,
        status="active",
        created_at="now",
        updated_at="now",
        merged_at=None,
    )
    result = await registry.call(
        "create_worktree",
        {
            "branch_name": "feature/remote",
            "worktree_path": "/tmp/wt/feature-remote",
        },
    )
    assert result["success"] is True
    mock_git_manager.has_unpushed_commits.assert_called_once_with("main")
    mock_git_manager.create_worktree.assert_called_once_with(
        worktree_path="/tmp/wt/feature-remote",
        branch_name="feature/remote",
        base_branch="main",
        create_branch=True,
        use_local=False,
    )
