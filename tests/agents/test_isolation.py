"""
Tests for Isolation Handlers.

Tests the isolation abstraction layer for spawn_agent unified API.
"""

from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.isolation import (
    CloneIsolationHandler,
    IsolationContext,
    IsolationHandler,
    NoneIsolationHandler,
    SpawnConfig,
    WorktreeIsolationHandler,
    generate_branch_name,
    get_isolation_handler,
)

pytestmark = pytest.mark.unit


class TestIsolationContext:
    """Tests for IsolationContext dataclass."""

    def test_isolation_context_fields(self) -> None:
        """Test IsolationContext has all required fields."""
        ctx = IsolationContext(
            cwd="/path/to/project",
            branch_name="feature-branch",
            worktree_id="wt-123",
            clone_id="clone-456",
            isolation_type="worktree",
        )

        assert ctx.cwd == "/path/to/project"
        assert ctx.branch_name == "feature-branch"
        assert ctx.worktree_id == "wt-123"
        assert ctx.clone_id == "clone-456"
        assert ctx.isolation_type == "worktree"

    def test_isolation_context_defaults(self) -> None:
        """Test IsolationContext default values."""
        ctx = IsolationContext(cwd="/path/to/project")

        assert ctx.cwd == "/path/to/project"
        assert ctx.branch_name is None
        assert ctx.worktree_id is None
        assert ctx.clone_id is None
        assert ctx.isolation_type == "none"
        assert ctx.extra == {}

    def test_isolation_context_extra_dict(self) -> None:
        """Test IsolationContext extra dict for additional metadata."""
        ctx = IsolationContext(
            cwd="/path/to/project",
            extra={"main_repo_path": "/path/to/main"},
        )

        assert ctx.extra["main_repo_path"] == "/path/to/main"


class TestSpawnConfig:
    """Tests for SpawnConfig dataclass."""

    def test_spawn_config_fields(self) -> None:
        """Test SpawnConfig has all required fields."""
        config = SpawnConfig(
            prompt="Test prompt",
            task_id="task-123",
            task_title="Implement feature",
            task_seq_num=6121,
            branch_name=None,
            branch_prefix="feat/",
            base_branch="main",
            project_id="proj-456",
            project_path="/path/to/project",
            provider="claude",
            parent_session_id="session-789",
        )

        assert config.prompt == "Test prompt"
        assert config.task_id == "task-123"
        assert config.task_title == "Implement feature"
        assert config.task_seq_num == 6121
        assert config.branch_name is None
        assert config.branch_prefix == "feat/"
        assert config.base_branch == "main"
        assert config.project_id == "proj-456"
        assert config.project_path == "/path/to/project"
        assert config.provider == "claude"
        assert config.parent_session_id == "session-789"


class TestGenerateBranchName:
    """Tests for generate_branch_name function."""

    def test_explicit_branch_name_returned(self) -> None:
        """Test explicit branch_name is returned as-is."""
        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-explicit-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        branch = generate_branch_name(config)
        assert branch == "my-explicit-branch"

    def test_branch_from_task_title(self) -> None:
        """Test branch generated from task title and seq_num."""
        config = SpawnConfig(
            prompt="Test",
            task_id="task-123",
            task_title="Implement Login Feature",
            task_seq_num=6079,
            branch_name=None,
            branch_prefix=None,
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        branch = generate_branch_name(config)
        assert branch == "task-6079-implement-login-feature"

    def test_branch_from_task_title_slug_truncated(self) -> None:
        """Test branch slug is truncated to 40 chars."""
        config = SpawnConfig(
            prompt="Test",
            task_id="task-123",
            task_title="This is a very long task title that should be truncated to forty characters",
            task_seq_num=6079,
            branch_name=None,
            branch_prefix=None,
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        branch = generate_branch_name(config)
        # Slug should be max 40 chars after "task-6079-"
        assert branch.startswith("task-6079-")
        slug_part = branch[len("task-6079-") :]
        assert len(slug_part) <= 40

    def test_branch_from_task_title_special_chars_removed(self) -> None:
        """Test special characters are removed from slug."""
        config = SpawnConfig(
            prompt="Test",
            task_id="task-123",
            task_title="Fix bug #123: Handle @user's input!",
            task_seq_num=6080,
            branch_name=None,
            branch_prefix=None,
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        branch = generate_branch_name(config)
        # Only alphanumeric and hyphens should remain
        assert branch == "task-6080-fix-bug-123-handle-users-input"

    def test_fallback_to_prefix_timestamp(self) -> None:
        """Test fallback to prefix+timestamp when no task."""
        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name=None,
            branch_prefix="agent/",
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        with patch("time.time", return_value=1706297600):
            branch = generate_branch_name(config)
            assert branch == "agent/1706297600"

    def test_fallback_default_prefix(self) -> None:
        """Test default prefix 'agent/' when no prefix specified."""
        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name=None,
            branch_prefix=None,
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        with patch("time.time", return_value=1706297600):
            branch = generate_branch_name(config)
            assert branch == "agent/1706297600"


class TestNoneIsolationHandler:
    """Tests for NoneIsolationHandler."""

    @pytest.mark.asyncio
    async def test_prepare_environment_returns_project_path(self) -> None:
        """Test prepare_environment returns IsolationContext with project_path as cwd."""
        handler = NoneIsolationHandler()
        config = SpawnConfig(
            prompt="Test prompt",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name=None,
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/my/project",
            provider="claude",
            parent_session_id="sess-456",
        )

        ctx = await handler.prepare_environment(config)

        assert ctx.cwd == "/path/to/my/project"
        assert ctx.isolation_type == "none"

    @pytest.mark.asyncio
    async def test_prepare_environment_no_branch_or_ids(self) -> None:
        """Test prepare_environment returns no branch, worktree_id, or clone_id."""
        handler = NoneIsolationHandler()
        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name=None,
            branch_prefix=None,
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        ctx = await handler.prepare_environment(config)

        assert ctx.branch_name is None
        assert ctx.worktree_id is None
        assert ctx.clone_id is None

    def test_build_context_prompt_returns_unchanged(self) -> None:
        """Test build_context_prompt returns original prompt unchanged."""
        handler = NoneIsolationHandler()
        original_prompt = "Please implement the login feature."
        ctx = IsolationContext(cwd="/path/to/project")

        result = handler.build_context_prompt(original_prompt, ctx)

        assert result == original_prompt

    @pytest.mark.asyncio
    async def test_cleanup_environment_is_noop(self) -> None:
        """Test cleanup_environment does nothing for current handler."""
        handler = NoneIsolationHandler()
        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name=None,
            branch_prefix=None,
            base_branch="main",
            project_id="proj",
            project_path="/path",
            provider="claude",
            parent_session_id="sess",
        )

        # Should not raise
        await handler.cleanup_environment(config)

    def test_is_isolation_handler_subclass(self) -> None:
        """Test NoneIsolationHandler is a subclass of IsolationHandler."""
        assert issubclass(NoneIsolationHandler, IsolationHandler)

    def test_isolation_handler_is_abstract(self) -> None:
        """Test IsolationHandler cannot be instantiated directly."""
        with pytest.raises(TypeError):
            IsolationHandler()  # type: ignore


class TestWorktreeIsolationHandler:
    """Tests for WorktreeIsolationHandler."""

    @pytest.mark.asyncio
    async def test_prepare_environment_creates_worktree(self) -> None:
        """Test prepare_environment creates worktree if not exists."""
        mock_git_manager = MagicMock()
        mock_git_manager.repo_path = "/path/to/main/repo"
        mock_git_manager.create_worktree.return_value = MagicMock(
            success=True,
            worktree_path="/tmp/worktrees/my-branch",
        )
        mock_git_manager.get_current_branch.return_value = "main"
        mock_git_manager.has_unpushed_commits.return_value = (False, 0)

        mock_worktree_storage = MagicMock()
        mock_worktree_storage.get_by_branch.return_value = None  # No existing worktree
        mock_worktree_storage.create.return_value = MagicMock(
            id="wt-123",
            worktree_path="/tmp/worktrees/my-branch",
            branch_name="my-branch",
        )

        handler = WorktreeIsolationHandler(
            git_manager=mock_git_manager,
            worktree_storage=mock_worktree_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        ctx = await handler.prepare_environment(config)

        assert ctx.isolation_type == "worktree"
        assert ctx.worktree_id == "wt-123"
        assert ctx.branch_name == "my-branch"
        mock_git_manager.create_worktree.assert_called_once()

    @pytest.mark.asyncio
    async def test_prepare_environment_reuses_existing_worktree(self) -> None:
        """Test prepare_environment reuses existing worktree for same branch."""
        mock_git_manager = MagicMock()
        mock_git_manager.repo_path = "/path/to/main/repo"

        mock_worktree_storage = MagicMock()
        mock_worktree_storage.get_by_branch.return_value = MagicMock(
            id="existing-wt-456",
            worktree_path="/tmp/worktrees/existing-branch",
            branch_name="existing-branch",
        )

        handler = WorktreeIsolationHandler(
            git_manager=mock_git_manager,
            worktree_storage=mock_worktree_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="existing-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        ctx = await handler.prepare_environment(config)

        assert ctx.worktree_id == "existing-wt-456"
        assert ctx.cwd == "/tmp/worktrees/existing-branch"
        # Should NOT create a new worktree
        mock_git_manager.create_worktree.assert_not_called()

    def test_build_context_prompt_prepends_warning(self) -> None:
        """Test build_context_prompt prepends CRITICAL: Worktree Context warning."""
        mock_git_manager = MagicMock()
        mock_worktree_storage = MagicMock()

        handler = WorktreeIsolationHandler(
            git_manager=mock_git_manager,
            worktree_storage=mock_worktree_storage,
        )

        original_prompt = "Please implement the login feature."
        ctx = IsolationContext(
            cwd="/tmp/worktrees/feature-branch",
            branch_name="feature-branch",
            worktree_id="wt-123",
            isolation_type="worktree",
            extra={"main_repo_path": "/path/to/main/repo"},
        )

        result = handler.build_context_prompt(original_prompt, ctx)

        assert "CRITICAL: Worktree Context" in result
        assert original_prompt in result
        assert "feature-branch" in result

    @pytest.mark.asyncio
    async def test_cleanup_after_storage_create_failure(self) -> None:
        """Test cleanup removes worktree on disk when storage.create fails."""
        mock_git_manager = MagicMock()
        mock_git_manager.repo_path = "/path/to/main/repo"
        mock_git_manager.create_worktree.return_value = MagicMock(success=True)
        mock_git_manager.get_current_branch.return_value = "main"
        mock_git_manager.has_unpushed_commits.return_value = (False, 0)

        mock_worktree_storage = MagicMock()
        mock_worktree_storage.get_by_branch.return_value = None
        mock_worktree_storage.create.side_effect = RuntimeError("DB error")

        handler = WorktreeIsolationHandler(
            git_manager=mock_git_manager,
            worktree_storage=mock_worktree_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        with pytest.raises(RuntimeError, match="DB error"):
            await handler.prepare_environment(config)

        # Handler should have tracked the worktree path but not the storage id
        assert handler._created_worktree_path is not None
        assert handler._created_worktree_id is None
        tracked_path = handler._created_worktree_path

        await handler.cleanup_environment(config)

        # Should have called delete_worktree to clean up disk
        mock_git_manager.delete_worktree.assert_called_once_with(
            worktree_path=tracked_path,
            force=True,
        )
        # State should be cleared after cleanup
        assert handler._created_worktree_path is None

    @pytest.mark.asyncio
    async def test_cleanup_after_hook_copy_failure(self) -> None:
        """Test cleanup removes worktree and storage record when hook copy fails."""
        mock_git_manager = MagicMock()
        mock_git_manager.repo_path = "/path/to/main/repo"
        mock_git_manager.create_worktree.return_value = MagicMock(success=True)
        mock_git_manager.get_current_branch.return_value = "main"
        mock_git_manager.has_unpushed_commits.return_value = (False, 0)

        mock_worktree_storage = MagicMock()
        mock_worktree_storage.get_by_branch.return_value = None
        mock_worktree_storage.create.return_value = MagicMock(
            id="wt-123",
            worktree_path="/tmp/worktrees/my-branch",
            branch_name="my-branch",
        )

        handler = WorktreeIsolationHandler(
            git_manager=mock_git_manager,
            worktree_storage=mock_worktree_storage,
        )

        # Make _copy_cli_hooks raise
        with patch.object(handler, "_copy_cli_hooks", side_effect=OSError("Permission denied")):
            config = SpawnConfig(
                prompt="Test",
                task_id=None,
                task_title=None,
                task_seq_num=None,
                branch_name="my-branch",
                branch_prefix=None,
                base_branch="main",
                project_id="proj-123",
                project_path="/path/to/main/repo",
                provider="claude",
                parent_session_id="sess-456",
            )

            with pytest.raises(OSError, match="Permission denied"):
                await handler.prepare_environment(config)

        # Both path and id should be tracked
        assert handler._created_worktree_path is not None
        assert handler._created_worktree_id == "wt-123"

        await handler.cleanup_environment(config)

        mock_git_manager.delete_worktree.assert_called_once()
        mock_worktree_storage.delete.assert_called_once_with("wt-123")

    @pytest.mark.asyncio
    async def test_cleanup_noop_on_success(self) -> None:
        """Test cleanup does nothing after successful prepare."""
        mock_git_manager = MagicMock()
        mock_git_manager.repo_path = "/path/to/main/repo"
        mock_git_manager.create_worktree.return_value = MagicMock(success=True)
        mock_git_manager.get_current_branch.return_value = "main"
        mock_git_manager.has_unpushed_commits.return_value = (False, 0)

        mock_worktree_storage = MagicMock()
        mock_worktree_storage.get_by_branch.return_value = None
        mock_worktree_storage.create.return_value = MagicMock(
            id="wt-123",
            worktree_path="/tmp/worktrees/my-branch",
            branch_name="my-branch",
        )

        handler = WorktreeIsolationHandler(
            git_manager=mock_git_manager,
            worktree_storage=mock_worktree_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        await handler.prepare_environment(config)

        # After success, partial state should be cleared
        assert handler._created_worktree_path is None
        assert handler._created_worktree_id is None

        await handler.cleanup_environment(config)

        # Should NOT call delete since nothing to clean up
        mock_git_manager.delete_worktree.assert_not_called()
        mock_worktree_storage.delete.assert_not_called()

    def test_is_isolation_handler_subclass(self) -> None:
        """Test WorktreeIsolationHandler is a subclass of IsolationHandler."""
        assert issubclass(WorktreeIsolationHandler, IsolationHandler)


class TestCloneIsolationHandler:
    """Tests for CloneIsolationHandler."""

    @pytest.mark.asyncio
    async def test_prepare_environment_creates_clone(self) -> None:
        """Test prepare_environment creates shallow clone if not exists."""
        mock_clone_manager = MagicMock()
        mock_clone_manager.create_clone.return_value = MagicMock(
            success=True,
            clone_path="/tmp/clones/my-branch",
        )

        mock_clone_storage = MagicMock()
        mock_clone_storage.get_by_branch.return_value = None  # No existing clone
        mock_clone_storage.create.return_value = MagicMock(
            id="clone-123",
            clone_path="/tmp/clones/my-branch",
            branch_name="my-branch",
        )

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        ctx = await handler.prepare_environment(config)

        assert ctx.isolation_type == "clone"
        assert ctx.clone_id == "clone-123"
        assert ctx.branch_name == "my-branch"
        mock_clone_manager.create_clone.assert_called_once()
        # Should default to shallow=True, use_local=False when no git_manager
        call_kwargs = mock_clone_manager.create_clone.call_args.kwargs
        assert call_kwargs.get("shallow") is True
        assert call_kwargs.get("use_local") is False

    @pytest.mark.asyncio
    async def test_prepare_environment_uses_local_with_unpushed_commits(self) -> None:
        """Test prepare_environment uses local clone when unpushed commits detected."""
        mock_clone_manager = MagicMock()
        mock_clone_manager.create_clone.return_value = MagicMock(
            success=True,
            clone_path="/tmp/clones/my-branch",
        )

        mock_clone_storage = MagicMock()
        mock_clone_storage.get_by_branch.return_value = None
        mock_clone_storage.create.return_value = MagicMock(
            id="clone-456",
            clone_path="/tmp/clones/my-branch",
            branch_name="my-branch",
        )

        mock_git_manager = MagicMock()
        mock_git_manager.get_current_branch.return_value = "main"
        mock_git_manager.has_unpushed_commits.return_value = (True, 3)

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
            git_manager=mock_git_manager,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        ctx = await handler.prepare_environment(config)

        assert ctx.isolation_type == "clone"
        assert ctx.clone_id == "clone-456"
        # Should use full clone from local when unpushed commits exist
        call_kwargs = mock_clone_manager.create_clone.call_args.kwargs
        assert call_kwargs.get("use_local") is True
        assert call_kwargs.get("shallow") is False

    @pytest.mark.asyncio
    async def test_prepare_environment_no_local_without_unpushed(self) -> None:
        """Test prepare_environment uses remote clone when no unpushed commits."""
        mock_clone_manager = MagicMock()
        mock_clone_manager.create_clone.return_value = MagicMock(
            success=True,
            clone_path="/tmp/clones/my-branch",
        )

        mock_clone_storage = MagicMock()
        mock_clone_storage.get_by_branch.return_value = None
        mock_clone_storage.create.return_value = MagicMock(
            id="clone-789",
            clone_path="/tmp/clones/my-branch",
            branch_name="my-branch",
        )

        mock_git_manager = MagicMock()
        mock_git_manager.get_current_branch.return_value = "main"
        mock_git_manager.has_unpushed_commits.return_value = (False, 0)

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
            git_manager=mock_git_manager,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        ctx = await handler.prepare_environment(config)

        assert ctx.isolation_type == "clone"
        # Should use shallow remote clone when no unpushed commits
        call_kwargs = mock_clone_manager.create_clone.call_args.kwargs
        assert call_kwargs.get("use_local") is False
        assert call_kwargs.get("shallow") is True

    @pytest.mark.asyncio
    async def test_prepare_environment_reuses_existing_clone(self) -> None:
        """Test prepare_environment reuses existing clone for same branch."""
        mock_clone_manager = MagicMock()

        mock_clone_storage = MagicMock()
        mock_clone_storage.get_by_branch.return_value = MagicMock(
            id="existing-clone-456",
            clone_path="/tmp/clones/existing-branch",
            branch_name="existing-branch",
        )

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="existing-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        ctx = await handler.prepare_environment(config)

        assert ctx.clone_id == "existing-clone-456"
        assert ctx.cwd == "/tmp/clones/existing-branch"
        # Should NOT create a new clone
        mock_clone_manager.create_clone.assert_not_called()

    def test_build_context_prompt_prepends_warning(self) -> None:
        """Test build_context_prompt prepends CRITICAL: Clone Context warning."""
        mock_clone_manager = MagicMock()
        mock_clone_storage = MagicMock()

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
        )

        original_prompt = "Please implement the login feature."
        ctx = IsolationContext(
            cwd="/tmp/clones/feature-branch",
            branch_name="feature-branch",
            clone_id="clone-123",
            isolation_type="clone",
            extra={"source_repo": "https://github.com/user/repo.git"},
        )

        result = handler.build_context_prompt(original_prompt, ctx)

        assert "CRITICAL: Clone Context" in result
        assert original_prompt in result
        assert "feature-branch" in result

    @pytest.mark.asyncio
    async def test_cleanup_after_storage_create_failure(self) -> None:
        """Test cleanup removes clone on disk when storage.create fails."""
        mock_clone_manager = MagicMock()
        mock_clone_manager.create_clone.return_value = MagicMock(success=True)

        mock_clone_storage = MagicMock()
        mock_clone_storage.get_by_branch.return_value = None
        mock_clone_storage.create.side_effect = RuntimeError("DB error")

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        with pytest.raises(RuntimeError, match="DB error"):
            await handler.prepare_environment(config)

        # Handler should have tracked the clone path but not the storage id
        assert handler._created_clone_path is not None
        assert handler._created_clone_id is None
        tracked_path = handler._created_clone_path

        await handler.cleanup_environment(config)

        mock_clone_manager.delete_clone.assert_called_once_with(
            clone_path=tracked_path,
            force=True,
        )
        # State should be cleared after cleanup
        assert handler._created_clone_path is None

    @pytest.mark.asyncio
    async def test_cleanup_after_hook_copy_failure(self) -> None:
        """Test cleanup removes clone and storage record when hook copy fails."""
        mock_clone_manager = MagicMock()
        mock_clone_manager.create_clone.return_value = MagicMock(success=True)

        mock_clone_storage = MagicMock()
        mock_clone_storage.get_by_branch.return_value = None
        mock_clone_storage.create.return_value = MagicMock(
            id="clone-123",
            clone_path="/tmp/clones/my-branch",
            branch_name="my-branch",
        )

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
        )

        with patch.object(handler, "_copy_cli_hooks", side_effect=OSError("Permission denied")):
            config = SpawnConfig(
                prompt="Test",
                task_id=None,
                task_title=None,
                task_seq_num=None,
                branch_name="my-branch",
                branch_prefix=None,
                base_branch="main",
                project_id="proj-123",
                project_path="/path/to/main/repo",
                provider="claude",
                parent_session_id="sess-456",
            )

            with pytest.raises(OSError, match="Permission denied"):
                await handler.prepare_environment(config)

        assert handler._created_clone_path is not None
        assert handler._created_clone_id == "clone-123"

        await handler.cleanup_environment(config)

        mock_clone_manager.delete_clone.assert_called_once()
        mock_clone_storage.delete.assert_called_once_with("clone-123")

    @pytest.mark.asyncio
    async def test_cleanup_noop_on_success(self) -> None:
        """Test cleanup does nothing after successful prepare."""
        mock_clone_manager = MagicMock()
        mock_clone_manager.create_clone.return_value = MagicMock(success=True)

        mock_clone_storage = MagicMock()
        mock_clone_storage.get_by_branch.return_value = None
        mock_clone_storage.create.return_value = MagicMock(
            id="clone-123",
            clone_path="/tmp/clones/my-branch",
            branch_name="my-branch",
        )

        handler = CloneIsolationHandler(
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
        )

        config = SpawnConfig(
            prompt="Test",
            task_id=None,
            task_title=None,
            task_seq_num=None,
            branch_name="my-branch",
            branch_prefix=None,
            base_branch="main",
            project_id="proj-123",
            project_path="/path/to/main/repo",
            provider="claude",
            parent_session_id="sess-456",
        )

        await handler.prepare_environment(config)

        assert handler._created_clone_path is None
        assert handler._created_clone_id is None

        await handler.cleanup_environment(config)

        mock_clone_manager.delete_clone.assert_not_called()
        mock_clone_storage.delete.assert_not_called()

    def test_is_isolation_handler_subclass(self) -> None:
        """Test CloneIsolationHandler is a subclass of IsolationHandler."""
        assert issubclass(CloneIsolationHandler, IsolationHandler)


class TestGetIsolationHandler:
    """Tests for get_isolation_handler factory function."""

    def test_get_isolation_handler_none(self) -> None:
        """Test get_isolation_handler('none') returns NoneIsolationHandler."""
        handler = get_isolation_handler("none")

        assert isinstance(handler, NoneIsolationHandler)

    def test_get_isolation_handler_worktree(self) -> None:
        """Test get_isolation_handler('worktree', ...) returns WorktreeIsolationHandler."""
        mock_git_manager = MagicMock()
        mock_worktree_storage = MagicMock()

        handler = get_isolation_handler(
            "worktree",
            git_manager=mock_git_manager,
            worktree_storage=mock_worktree_storage,
        )

        assert isinstance(handler, WorktreeIsolationHandler)

    def test_get_isolation_handler_clone(self) -> None:
        """Test get_isolation_handler('clone', ...) returns CloneIsolationHandler."""
        mock_clone_manager = MagicMock()
        mock_clone_storage = MagicMock()

        handler = get_isolation_handler(
            "clone",
            clone_manager=mock_clone_manager,
            clone_storage=mock_clone_storage,
        )

        assert isinstance(handler, CloneIsolationHandler)

    def test_get_isolation_handler_invalid_mode_raises(self) -> None:
        """Test get_isolation_handler raises ValueError for invalid mode."""
        with pytest.raises(ValueError, match="Unknown isolation mode"):
            get_isolation_handler("invalid")

    def test_get_isolation_handler_worktree_missing_deps_raises(self) -> None:
        """Test get_isolation_handler('worktree') raises if dependencies missing."""
        with pytest.raises(ValueError, match="git_manager.*required"):
            get_isolation_handler("worktree")

    def test_get_isolation_handler_clone_missing_deps_raises(self) -> None:
        """Test get_isolation_handler('clone') raises if dependencies missing."""
        with pytest.raises(ValueError, match="clone_manager.*required"):
            get_isolation_handler("clone")
