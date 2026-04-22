from unittest.mock import MagicMock

import pytest

from gobby.mcp_proxy.tools.worktrees import create_worktrees_registry


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
