"""
Tests for Isolation Handlers.

Tests the isolation abstraction layer for spawn_agent unified API.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.isolation import (
    CloneIsolationHandler,
    IsolationContext,
    IsolationHandler,
    NoneIsolationHandler,
    SpawnConfig,
    WorktreeIsolationHandler,
    _patch_mcp_config_for_isolation,
    ensure_isolation_code_index,
    generate_branch_name,
    get_isolation_handler,
    provider_mcp_config_error,
    repair_isolation_environment,
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


class TestEnsureIsolationCodeIndex:
    """Tests for pre-spawn gcode indexing in isolated workspaces."""

    @staticmethod
    def _proc(returncode: int = 0, stderr: bytes = b"") -> AsyncMock:
        proc = AsyncMock()
        proc.returncode = returncode
        proc.communicate.return_value = (b"", stderr)
        return proc

    @pytest.mark.asyncio
    async def test_runs_gcode_index_in_workspace(self, tmp_path: Path) -> None:
        proc = self._proc()

        with (
            patch("gobby.agents.code_index.resolve_native_bin", return_value="/tmp/gcode"),
            patch(
                "gobby.agents.code_index.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ) as create_proc,
        ):
            result = await ensure_isolation_code_index(str(tmp_path))

        assert result.env == {}
        assert create_proc.await_count == 3
        calls = create_proc.await_args_list
        assert calls[0].args[:4] == ("/tmp/gcode", "projects", "--quiet", "--format")
        assert calls[1].args[:4] == ("/tmp/gcode", "index", "--quiet", "--project")
        assert calls[1].args[4] == str(tmp_path)
        assert calls[2].args[:3] == ("/tmp/gcode", "search-content", "__gobby_code_index_smoke__")
        assert calls[0].kwargs["cwd"] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_database_url_creates_gcode_wrapper_runtime(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        proc = self._proc()
        runtime_root = tmp_path / "runtime"
        workspace = tmp_path / "workspace"
        source_home = tmp_path / "home"
        monkeypatch.setenv("GOBBY_HOME", str(source_home))
        workspace.mkdir()
        subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)

        with (
            patch("gobby.agents.code_index.resolve_native_bin", return_value="/tmp/gcode"),
            patch(
                "gobby.agents.code_index.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ) as create_proc,
        ):
            result = await ensure_isolation_code_index(
                str(workspace),
                database_url="postgresql://gobby:secret@localhost/gobby",
                daemon_bind_host="127.0.0.1",
                daemon_port=61234,
                runtime_root=runtime_root,
            )

        wrapper = workspace / ".gobby" / "bin" / "gcode"
        assert result.wrapper_path == str(wrapper)
        assert result.runtime_home is not None
        assert result.env["PATH"].split(":")[0] == str(wrapper.parent)
        assert wrapper.read_text() == (
            f'#!/bin/sh\nexport GOBBY_HOME={result.runtime_home}\nexec /tmp/gcode "$@"\n'
        )
        bootstrap = Path(result.runtime_home) / "bootstrap.yaml"
        bootstrap_text = bootstrap.read_text()
        assert "database_url: postgresql://gobby:secret@localhost/gobby" in bootstrap_text
        assert "database_url_ref" not in bootstrap_text
        assert "bind_host: 127.0.0.1" in bootstrap_text
        assert "daemon_port: 61234" in bootstrap_text
        assert create_proc.await_args_list[0].args[0] == str(wrapper)
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        assert status.stdout == ""

    @pytest.mark.asyncio
    async def test_raises_when_gcode_index_fails(self, tmp_path: Path) -> None:
        proc_ok = self._proc()
        proc_fail = self._proc(returncode=2, stderr=b"parse failed")

        with (
            patch("gobby.agents.code_index.resolve_native_bin", return_value="/tmp/gcode"),
            patch(
                "gobby.agents.code_index.asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=[proc_ok, proc_fail]),
            ),
        ):
            with pytest.raises(RuntimeError, match="gcode_index_failed:2:parse failed"):
                await ensure_isolation_code_index(str(tmp_path))


class TestRepairIsolationEnvironment:
    """Tests for shared isolated workspace repair."""

    @pytest.mark.asyncio
    async def test_preseeds_python_environment(self, tmp_path: Path) -> None:
        with (
            patch("gobby.agents.isolation._copy_cli_hooks", new=AsyncMock()),
            patch("gobby.utils.project_context.ensure_project_json_for_isolation"),
            patch(
                "gobby.agents.isolation.preseed_isolated_python_environment", new=AsyncMock()
            ) as preseed,
            patch("gobby.agents.isolation._patch_mcp_config_for_isolation", new=AsyncMock()),
        ):
            result = await repair_isolation_environment(
                main_repo_path="/main/repo",
                isolated_path=str(tmp_path),
                provider="codex",
            )

        assert result is None
        assert tmp_path.exists()
        preseed.assert_awaited_once_with(str(tmp_path))


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

        result = await handler.cleanup_environment(config)
        assert result is None
        assert handler.build_context_prompt("prompt", IsolationContext(cwd="/path")) == "prompt"

    def test_is_isolation_handler_subclass(self) -> None:
        """Test NoneIsolationHandler is a subclass of IsolationHandler."""
        assert issubclass(NoneIsolationHandler, IsolationHandler)

    def test_isolation_handler_is_abstract(self) -> None:
        """Test IsolationHandler cannot be instantiated directly."""
        with pytest.raises(TypeError):
            IsolationHandler()


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
        mock_git_manager.get_current_branch.return_value = "main"

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

        with (
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "gobby.agents.isolation.repair_isolation_environment",
                new=AsyncMock(),
            ) as repair,
            patch(
                "gobby.agents.isolation.sync_reused_worktree_to_base",
                new=AsyncMock(),
            ) as sync,
        ):
            ctx = await handler.prepare_environment(config)

        assert ctx.worktree_id == "existing-wt-456"
        assert ctx.cwd == "/tmp/worktrees/existing-branch"
        sync.assert_awaited_once_with(
            git_manager=mock_git_manager,
            worktree_path="/tmp/worktrees/existing-branch",
            base_branch="main",
        )
        repair.assert_awaited_once_with(
            main_repo_path="/path/to/main/repo",
            isolated_path="/tmp/worktrees/existing-branch",
            provider="claude",
        )
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
        with patch(
            "gobby.agents.isolation._copy_cli_hooks", side_effect=OSError("Permission denied")
        ):
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

    @pytest.mark.asyncio
    async def test_prepare_calls_ensure_project_json(self) -> None:
        """Test prepare_environment calls ensure_project_json_for_isolation."""
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

        with patch("gobby.utils.project_context.ensure_project_json_for_isolation") as mock_ensure:
            await handler.prepare_environment(config)
            mock_ensure.assert_called_once_with(
                "/path/to/main/repo",
                handler._generate_worktree_path("my-branch", "repo"),
            )
            assert mock_ensure.call_count == 1
            assert mock_ensure.call_args is not None

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

        with (
            patch("pathlib.Path.is_dir", return_value=True),
            patch(
                "gobby.agents.isolation.repair_isolation_environment",
                new=AsyncMock(),
            ) as repair,
        ):
            ctx = await handler.prepare_environment(config)

        assert ctx.clone_id == "existing-clone-456"
        assert ctx.cwd == "/tmp/clones/existing-branch"
        repair.assert_awaited_once_with(
            main_repo_path="/path/to/main/repo",
            isolated_path="/tmp/clones/existing-branch",
            provider="claude",
        )
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

        with patch(
            "gobby.agents.isolation._copy_cli_hooks", side_effect=OSError("Permission denied")
        ):
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

    @pytest.mark.asyncio
    async def test_prepare_calls_ensure_project_json(self) -> None:
        """Test prepare_environment calls ensure_project_json_for_isolation."""
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
            project_path="/path/to/source/repo",
            provider="gemini",
            parent_session_id="sess-456",
        )

        with patch("gobby.utils.project_context.ensure_project_json_for_isolation") as mock_ensure:
            await handler.prepare_environment(config)
            mock_ensure.assert_called_once_with(
                "/path/to/source/repo",
                handler._generate_clone_path("my-branch", "repo"),
            )
            assert mock_ensure.call_count == 1
            assert mock_ensure.call_args is not None

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


class TestPatchMcpConfigForIsolation:
    """Tests for _patch_mcp_config_for_isolation."""

    @pytest.mark.asyncio
    async def test_writes_mcp_json(self, tmp_path: Path) -> None:
        """Writes .mcp.json with --project pointing to main repo."""
        isolated_path = str(tmp_path / "worktree")
        Path(isolated_path).mkdir()
        main_repo = "/path/to/main/repo"

        await _patch_mcp_config_for_isolation(main_repo, isolated_path, "gemini")

        mcp_json = Path(isolated_path) / ".mcp.json"
        assert mcp_json.exists()
        data = json.loads(mcp_json.read_text())
        gobby_server = data["mcpServers"]["gobby"]
        assert gobby_server["command"] == "uv"
        assert "--project" in gobby_server["args"]
        assert main_repo in gobby_server["args"]
        assert "gobby" in gobby_server["args"]
        assert "mcp-server" in gobby_server["args"]

    @pytest.mark.asyncio
    async def test_patches_claude_json_for_claude_provider(self, tmp_path: Path) -> None:
        """For claude provider, registers isolated path in ~/.claude.json."""
        isolated_path = str(tmp_path / "worktree")
        Path(isolated_path).mkdir()
        main_repo = "/path/to/main/repo"

        fake_claude_json = tmp_path / ".claude.json"
        fake_claude_json.write_text("{}")

        with patch("pathlib.Path.home", return_value=tmp_path):
            await _patch_mcp_config_for_isolation(main_repo, isolated_path, "claude")

        data = json.loads(fake_claude_json.read_text())
        assert isolated_path in data["projects"]
        project_config = data["projects"][isolated_path]
        assert "gobby" in project_config["mcpServers"]

    @pytest.mark.asyncio
    async def test_does_not_patch_claude_json_for_gemini(self, tmp_path: Path) -> None:
        """For non-claude provider, does not touch ~/.claude.json."""
        isolated_path = str(tmp_path / "worktree")
        Path(isolated_path).mkdir()

        fake_claude_json = tmp_path / ".claude.json"
        # File doesn't exist initially

        with patch("pathlib.Path.home", return_value=tmp_path):
            await _patch_mcp_config_for_isolation("/main", isolated_path, "gemini")

        # Should NOT have created ~/.claude.json
        assert not fake_claude_json.exists()

    @pytest.mark.asyncio
    async def test_preserves_existing_claude_json_data(self, tmp_path: Path) -> None:
        """Patching should preserve existing data in ~/.claude.json."""
        isolated_path = str(tmp_path / "worktree")
        Path(isolated_path).mkdir()

        fake_claude_json = tmp_path / ".claude.json"
        existing = {"existingKey": "value", "projects": {"/other": {"foo": "bar"}}}
        fake_claude_json.write_text(json.dumps(existing))

        with patch("pathlib.Path.home", return_value=tmp_path):
            await _patch_mcp_config_for_isolation("/main", isolated_path, "claude")

        data = json.loads(fake_claude_json.read_text())
        assert data["existingKey"] == "value"
        assert "/other" in data["projects"]
        assert isolated_path in data["projects"]

    @pytest.mark.asyncio
    async def test_handles_write_failure_gracefully(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Should log warning but not raise on write failure."""
        # Non-existent parent dir will cause write failure
        isolated_path = str(tmp_path / "nonexistent" / "deep" / "path")

        # Should not raise
        await _patch_mcp_config_for_isolation("/main", isolated_path, "claude")

        # Verify warning was logged
        assert any("Failed to write" in msg for msg in caplog.messages)


class TestProviderMcpConfigPreflight:
    """Tests for provider_mcp_config_error."""

    def test_reports_missing_mcp_json(self, tmp_path: Path) -> None:
        assert provider_mcp_config_error(str(tmp_path), "gemini").startswith(
            "provider_mcp_config_missing:"
        )

    def test_accepts_non_claude_mcp_json(self, tmp_path: Path) -> None:
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "gobby": {
                            "command": "uv",
                            "args": ["run", "--project", "/main", "gobby", "mcp-server"],
                        }
                    }
                }
            )
        )

        assert provider_mcp_config_error(str(tmp_path), "gemini") is None

    def test_requires_claude_project_config(self, tmp_path: Path) -> None:
        (tmp_path / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "gobby": {
                            "command": "uv",
                            "args": ["run", "--project", "/main", "gobby", "mcp-server"],
                        }
                    }
                }
            )
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            error = provider_mcp_config_error(str(tmp_path), "claude")

        assert error is not None
        assert error.startswith("provider_mcp_config_missing:")
