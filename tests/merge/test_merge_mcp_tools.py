"""Tests for gobby-merge MCP server tools (TDD green phase).

Tests for MCP tools in gobby-merge server:
- merge_start: Initiate merge with AI resolution
- merge_status: Get current merge state and conflicts
- merge_resolve: Apply AI resolution to specific conflict
- merge_apply: Apply all resolutions and complete merge
- merge_abort: Cancel merge and restore state
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry

pytestmark = pytest.mark.unit

# ==============================================================================
# Import Tests - verify module structure
# ==============================================================================


class TestMergeToolsImports:
    """Test that gobby-merge MCP tools module can be imported."""

    def test_import_merge_tools_module(self) -> None:
        """Can import merge tools module."""
        from gobby.mcp_proxy.tools import merge  # noqa: F401

    def test_import_create_merge_registry(self) -> None:
        """Can import create_merge_registry function."""
        from gobby.mcp_proxy.tools.merge import create_merge_registry

        assert callable(create_merge_registry)

    def test_import_merge_tool_names(self) -> None:
        """Registry exposes expected tool names."""
        from gobby.mcp_proxy.tools.merge import create_merge_registry

        # Create registry with mock dependencies
        mock_storage = MagicMock()
        mock_resolver = MagicMock()
        mock_git_manager = MagicMock()

        registry = create_merge_registry(
            merge_storage=mock_storage,
            merge_resolver=mock_resolver,
            git_manager=mock_git_manager,
        )

        # Registry should have the expected tools
        tool_names = [t["name"] for t in registry.list_tools()]
        assert "merge_start" in tool_names
        assert "merge_status" in tool_names
        assert "merge_resolve" in tool_names
        assert "merge_apply" in tool_names
        assert "merge_abort" in tool_names


# ==============================================================================
# Registry Creation Tests
# ==============================================================================


class TestMergeRegistryCreation:
    """Tests for merge registry creation."""

    def test_registry_has_correct_name(self) -> None:
        """Registry has name 'gobby-merge'."""
        from gobby.mcp_proxy.tools.merge import create_merge_registry

        mock_storage = MagicMock()
        mock_resolver = MagicMock()
        mock_git_manager = MagicMock()

        registry = create_merge_registry(
            merge_storage=mock_storage,
            merge_resolver=mock_resolver,
            git_manager=mock_git_manager,
        )

        assert isinstance(registry, InternalToolRegistry)
        assert registry.name == "gobby-merge"

    def test_registry_has_description(self) -> None:
        """Registry has a description."""
        from gobby.mcp_proxy.tools.merge import create_merge_registry

        mock_storage = MagicMock()
        mock_resolver = MagicMock()
        mock_git_manager = MagicMock()

        registry = create_merge_registry(
            merge_storage=mock_storage,
            merge_resolver=mock_resolver,
            git_manager=mock_git_manager,
        )

        assert registry.description is not None
        assert len(registry.description) > 0


# ==============================================================================
# merge_start Tool Tests
# ==============================================================================


class TestMergeStartTool:
    """Tests for merge_start tool."""

    @pytest.fixture
    def mock_storage(self):
        """Create mock merge resolution storage."""
        storage = MagicMock()
        storage.create_resolution = MagicMock()
        storage.get_resolution = MagicMock()
        storage.update_resolution = MagicMock()
        storage.create_conflict = MagicMock()
        storage.list_resolutions = MagicMock(return_value=[])
        return storage

    @pytest.fixture
    def mock_resolver(self):
        """Create mock merge resolver."""
        resolver = MagicMock()
        resolver.resolve = AsyncMock()
        return resolver

    @pytest.fixture
    def mock_git_manager(self):
        """Create mock git manager."""
        git_manager = MagicMock()
        git_manager.repo_path = "/test/repo"
        return git_manager

    @pytest.fixture
    def mock_worktree_manager(self):
        """Create mock worktree manager."""
        worktree_manager = MagicMock()
        mock_worktree = MagicMock()
        mock_worktree.worktree_path = "/test/repo/worktrees/wt-abc"
        worktree_manager.get_worktree.return_value = mock_worktree
        return worktree_manager

    @pytest.fixture
    def merge_registry(self, mock_storage, mock_resolver, mock_git_manager, mock_worktree_manager):
        """Create merge registry with mocked dependencies."""
        from gobby.mcp_proxy.tools.merge import create_merge_registry

        return create_merge_registry(
            merge_storage=mock_storage,
            merge_resolver=mock_resolver,
            git_manager=mock_git_manager,
            worktree_manager=mock_worktree_manager,
        )

    @pytest.mark.asyncio
    async def test_merge_start_creates_resolution(
        self, merge_registry, mock_storage, mock_resolver
    ):
        """merge_start creates a new resolution record."""
        from gobby.storage.merge_resolutions import MergeResolution
        from gobby.worktrees.merge import MergeResult, ResolutionTier

        # Mock successful auto-merge
        mock_resolver.resolve.return_value = MergeResult(
            success=True,
            tier=ResolutionTier.GIT_AUTO,
            conflicts=[],
            resolved_files=[],
            unresolved_conflicts=[],
            needs_human_review=False,
        )

        mock_resolution = MergeResolution(
            id="mr-test123",
            worktree_id="wt-abc",
            source_branch="feature/test",
            target_branch="main",
            status="pending",
            tier_used=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.create_resolution.return_value = mock_resolution

        result = await merge_registry.call(
            "merge_start",
            {
                "worktree_id": "wt-abc",
                "source_branch": "feature/test",
                "target_branch": "main",
            },
        )

        assert result["success"] is True
        assert "resolution_id" in result
        mock_storage.create_resolution.assert_called_once()

    @pytest.mark.asyncio
    async def test_merge_start_with_conflicts(self, merge_registry, mock_storage, mock_resolver):
        """merge_start reports conflicts when merge has conflicts."""
        from gobby.storage.merge_resolutions import MergeResolution
        from gobby.worktrees.merge import MergeResult, ResolutionTier

        # Mock merge with conflicts
        mock_resolver.resolve.return_value = MergeResult(
            success=False,
            tier=ResolutionTier.HUMAN_REVIEW,
            conflicts=[{"file": "src/test.py", "hunks": []}],
            resolved_files=[],
            unresolved_conflicts=[{"file": "src/test.py", "hunks": []}],
            needs_human_review=True,
        )

        mock_resolution = MergeResolution(
            id="mr-test123",
            worktree_id="wt-abc",
            source_branch="feature/test",
            target_branch="main",
            status="pending",
            tier_used=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.create_resolution.return_value = mock_resolution

        result = await merge_registry.call(
            "merge_start",
            {
                "worktree_id": "wt-abc",
                "source_branch": "feature/test",
                "target_branch": "main",
            },
        )

        assert result["success"] is False
        assert result["needs_human_review"] is True
        assert len(result["conflicts"]) > 0

    @pytest.mark.asyncio
    async def test_merge_start_requires_worktree_id(self, merge_registry):
        """merge_start requires worktree_id parameter."""
        result = await merge_registry.call(
            "merge_start",
            {
                "worktree_id": "",
                "source_branch": "feature/test",
                "target_branch": "main",
            },
        )

        assert result["success"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_merge_start_requires_source_branch(self, merge_registry):
        """merge_start requires source_branch parameter."""
        result = await merge_registry.call(
            "merge_start",
            {
                "worktree_id": "wt-abc",
                "source_branch": "",
                "target_branch": "main",
            },
        )

        assert result["success"] is False
        assert "error" in result


# ==============================================================================
# merge_status Tool Tests
# ==============================================================================


class TestMergeStatusTool:
    """Tests for merge_status tool."""

    @pytest.fixture
    def mock_storage(self):
        """Create mock merge resolution storage."""
        storage = MagicMock()
        storage.get_resolution = MagicMock()
        storage.list_conflicts = MagicMock()
        return storage

    @pytest.fixture
    def mock_resolver(self):
        """Create mock merge resolver."""
        return MagicMock()

    @pytest.fixture
    def mock_git_manager(self):
        """Create mock git manager."""
        return MagicMock()

    @pytest.fixture
    def merge_registry(self, mock_storage, mock_resolver, mock_git_manager):
        """Create merge registry with mocked dependencies."""
        from gobby.mcp_proxy.tools.merge import create_merge_registry

        return create_merge_registry(
            merge_storage=mock_storage,
            merge_resolver=mock_resolver,
            git_manager=mock_git_manager,
        )

    @pytest.mark.asyncio
    async def test_merge_status_returns_resolution_info(self, merge_registry, mock_storage):
        """merge_status returns resolution details."""
        from gobby.storage.merge_resolutions import MergeResolution

        mock_resolution = MergeResolution(
            id="mr-test123",
            worktree_id="wt-abc",
            source_branch="feature/test",
            target_branch="main",
            status="pending",
            tier_used=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.get_resolution.return_value = mock_resolution
        mock_storage.list_conflicts.return_value = []

        result = await merge_registry.call("merge_status", {"resolution_id": "mr-test123"})

        assert result["success"] is True
        assert result["resolution"]["id"] == "mr-test123"
        assert result["resolution"]["status"] == "pending"

    @pytest.mark.asyncio
    async def test_merge_status_includes_conflicts(self, merge_registry, mock_storage):
        """merge_status includes conflict details."""
        from gobby.storage.merge_resolutions import MergeConflict, MergeResolution

        mock_resolution = MergeResolution(
            id="mr-test123",
            worktree_id="wt-abc",
            source_branch="feature/test",
            target_branch="main",
            status="pending",
            tier_used=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_conflicts = [
            MergeConflict(
                id="mc-conflict1",
                resolution_id="mr-test123",
                file_path="src/test.py",
                status="pending",
                ours_content="our version",
                theirs_content="their version",
                resolved_content=None,
                created_at="2025-01-01T00:00:00+00:00",
                updated_at="2025-01-01T00:00:00+00:00",
            )
        ]
        mock_storage.get_resolution.return_value = mock_resolution
        mock_storage.list_conflicts.return_value = mock_conflicts

        result = await merge_registry.call("merge_status", {"resolution_id": "mr-test123"})

        assert result["success"] is True
        assert len(result["conflicts"]) == 1
        assert result["conflicts"][0]["file_path"] == "src/test.py"

    @pytest.mark.asyncio
    async def test_merge_status_not_found(self, merge_registry, mock_storage):
        """merge_status returns error for unknown resolution."""
        mock_storage.get_resolution.return_value = None

        result = await merge_registry.call("merge_status", {"resolution_id": "mr-unknown"})

        assert result["success"] is False
        assert "not found" in result["error"].lower()


# ==============================================================================
# merge_resolve Tool Tests
# ==============================================================================


class TestMergeResolveTool:
    """Tests for merge_resolve tool."""

    @pytest.fixture
    def mock_storage(self):
        """Create mock merge resolution storage."""
        storage = MagicMock()
        storage.get_conflict = MagicMock()
        storage.update_conflict = MagicMock()
        storage.get_resolution = MagicMock()
        return storage

    @pytest.fixture
    def mock_resolver(self):
        """Create mock merge resolver."""
        resolver = MagicMock()
        resolver.resolve_file = AsyncMock()
        return resolver

    @pytest.fixture
    def mock_git_manager(self):
        """Create mock git manager."""
        return MagicMock()

    @pytest.fixture
    def merge_registry(self, mock_storage, mock_resolver, mock_git_manager):
        """Create merge registry with mocked dependencies."""
        from gobby.mcp_proxy.tools.merge import create_merge_registry

        return create_merge_registry(
            merge_storage=mock_storage,
            merge_resolver=mock_resolver,
            git_manager=mock_git_manager,
        )

    @pytest.mark.asyncio
    async def test_merge_resolve_applies_ai_resolution(
        self, merge_registry, mock_storage, mock_resolver
    ):
        """merge_resolve applies AI resolution to conflict."""
        from gobby.storage.merge_resolutions import MergeConflict
        from gobby.worktrees.merge import ResolutionResult, ResolutionTier

        mock_conflict = MergeConflict(
            id="mc-conflict1",
            resolution_id="mr-test123",
            file_path="src/test.py",
            status="pending",
            ours_content="our version",
            theirs_content="their version",
            resolved_content=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.get_conflict.return_value = mock_conflict

        # Mock successful AI resolution with the resolved content propagated.
        mock_resolver.resolve_file.return_value = ResolutionResult(
            success=True,
            tier=ResolutionTier.CONFLICT_ONLY_AI,
            conflicts=[],
            resolved_files=["src/test.py"],
            unresolved_conflicts=[],
            needs_human_review=False,
            resolved_content_by_file={"src/test.py": "merged version"},
        )

        resolved_conflict = MergeConflict(
            id="mc-conflict1",
            resolution_id="mr-test123",
            file_path="src/test.py",
            status="resolved",
            ours_content="our version",
            theirs_content="their version",
            resolved_content="merged version",
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.update_conflict.return_value = resolved_conflict

        result = await merge_registry.call("merge_resolve", {"conflict_id": "mc-conflict1"})

        assert result["success"] is True
        assert result["conflict"]["status"] == "resolved"
        mock_resolver.resolve_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_merge_resolve_with_manual_content(self, merge_registry, mock_storage):
        """merge_resolve accepts manual resolved content."""
        from gobby.storage.merge_resolutions import MergeConflict

        mock_conflict = MergeConflict(
            id="mc-conflict1",
            resolution_id="mr-test123",
            file_path="src/test.py",
            status="pending",
            ours_content="our version",
            theirs_content="their version",
            resolved_content=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.get_conflict.return_value = mock_conflict

        resolved_conflict = MergeConflict(
            id="mc-conflict1",
            resolution_id="mr-test123",
            file_path="src/test.py",
            status="resolved",
            ours_content="our version",
            theirs_content="their version",
            resolved_content="manual merge",
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.update_conflict.return_value = resolved_conflict

        result = await merge_registry.call(
            "merge_resolve",
            {
                "conflict_id": "mc-conflict1",
                "resolved_content": "manual merge",
            },
        )

        assert result["success"] is True
        mock_storage.update_conflict.assert_called_once()

    @pytest.mark.asyncio
    async def test_merge_resolve_conflict_not_found(self, merge_registry, mock_storage):
        """merge_resolve returns error for unknown conflict."""
        mock_storage.get_conflict.return_value = None

        result = await merge_registry.call("merge_resolve", {"conflict_id": "mc-unknown"})

        assert result["success"] is False
        assert "not found" in result["error"].lower()


# ==============================================================================
# merge_apply Tool Tests
# ==============================================================================


class TestMergeApplyTool:
    """Tests for merge_apply tool."""

    @pytest.fixture
    def mock_storage(self):
        """Create mock merge resolution storage."""
        storage = MagicMock()
        storage.get_resolution = MagicMock()
        storage.list_conflicts = MagicMock()
        storage.update_resolution = MagicMock()
        return storage

    @pytest.fixture
    def mock_resolver(self):
        """Create mock merge resolver."""
        return MagicMock()

    @pytest.fixture
    def mock_git_manager(self):
        """Create mock git manager. _run_git returns subprocess-like records."""
        git_manager = MagicMock()
        git_manager._run_git = MagicMock()
        return git_manager

    @pytest.fixture
    def mock_worktree_manager(self, tmp_path):
        """Mock worktree manager whose `get` returns a worktree under tmp_path."""
        manager = MagicMock()
        worktree = MagicMock()
        worktree.worktree_path = str(tmp_path)
        manager.get.return_value = worktree
        return manager

    @pytest.fixture
    def merge_registry(self, mock_storage, mock_resolver, mock_git_manager, mock_worktree_manager):
        """Create merge registry with mocked dependencies."""
        from gobby.mcp_proxy.tools.merge import create_merge_registry

        return create_merge_registry(
            merge_storage=mock_storage,
            merge_resolver=mock_resolver,
            git_manager=mock_git_manager,
            worktree_manager=mock_worktree_manager,
        )

    @pytest.mark.asyncio
    async def test_merge_apply_all_resolved(
        self, merge_registry, mock_storage, mock_git_manager, tmp_path
    ):
        """merge_apply writes resolved content, stages, and commits the merge."""
        from gobby.storage.merge_resolutions import MergeConflict, MergeResolution

        mock_resolution = MergeResolution(
            id="mr-test123",
            worktree_id="wt-abc",
            source_branch="feature/test",
            target_branch="main",
            status="pending",
            tier_used=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.get_resolution.return_value = mock_resolution

        resolved_conflicts = [
            MergeConflict(
                id="mc-conflict1",
                resolution_id="mr-test123",
                file_path="src/test.py",
                status="resolved",
                ours_content="our version",
                theirs_content="their version",
                resolved_content="merged version\n",
                created_at="2025-01-01T00:00:00+00:00",
                updated_at="2025-01-01T00:00:00+00:00",
            )
        ]
        mock_storage.list_conflicts.return_value = resolved_conflicts

        def fake_run_git(args, cwd=None, timeout=None):
            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            if args[:1] == ["diff"]:
                result.stdout = ""
            else:
                result.stdout = ""
            return result

        mock_git_manager._run_git.side_effect = fake_run_git

        updated_resolution = MergeResolution(
            id="mr-test123",
            worktree_id="wt-abc",
            source_branch="feature/test",
            target_branch="main",
            status="resolved",
            tier_used="conflict_only_ai",
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.update_resolution.return_value = updated_resolution

        result = await merge_registry.call("merge_apply", {"resolution_id": "mr-test123"})

        assert result["success"] is True
        assert result["resolution"]["status"] == "resolved"
        # Resolved content was written to disk under the worktree path.
        written = (tmp_path / "src" / "test.py").read_text()
        assert written == "merged version\n"
        # git add + diff + commit were issued in that order.
        called_cmds = [call.args[0][:2] for call in mock_git_manager._run_git.call_args_list]
        assert called_cmds == [["add", "--"], ["diff", "--name-only"], ["commit", "--no-edit"]]

    @pytest.mark.asyncio
    async def test_merge_apply_with_pending_conflicts(self, merge_registry, mock_storage):
        """merge_apply fails when conflicts are unresolved."""
        from gobby.storage.merge_resolutions import MergeConflict, MergeResolution

        mock_resolution = MergeResolution(
            id="mr-test123",
            worktree_id="wt-abc",
            source_branch="feature/test",
            target_branch="main",
            status="pending",
            tier_used=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.get_resolution.return_value = mock_resolution

        # One conflict still pending
        conflicts = [
            MergeConflict(
                id="mc-conflict1",
                resolution_id="mr-test123",
                file_path="src/test.py",
                status="pending",
                ours_content="our version",
                theirs_content="their version",
                resolved_content=None,
                created_at="2025-01-01T00:00:00+00:00",
                updated_at="2025-01-01T00:00:00+00:00",
            )
        ]
        mock_storage.list_conflicts.return_value = conflicts

        result = await merge_registry.call("merge_apply", {"resolution_id": "mr-test123"})

        assert result["success"] is False
        assert "unresolved" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_merge_apply_resolution_not_found(self, merge_registry, mock_storage):
        """merge_apply returns error for unknown resolution."""
        mock_storage.get_resolution.return_value = None

        result = await merge_registry.call("merge_apply", {"resolution_id": "mr-unknown"})

        assert result["success"] is False
        assert "not found" in result["error"].lower()


# ==============================================================================
# merge_abort Tool Tests
# ==============================================================================


class TestMergeAbortTool:
    """Tests for merge_abort tool."""

    @pytest.fixture
    def mock_storage(self):
        """Create mock merge resolution storage."""
        storage = MagicMock()
        storage.get_resolution = MagicMock()
        storage.update_resolution = MagicMock()
        storage.delete_resolution = MagicMock()
        return storage

    @pytest.fixture
    def mock_resolver(self):
        """Create mock merge resolver."""
        return MagicMock()

    @pytest.fixture
    def mock_git_manager(self):
        """Create mock git manager."""
        git_manager = MagicMock()
        git_manager.abort_merge = MagicMock()
        return git_manager

    @pytest.fixture
    def merge_registry(self, mock_storage, mock_resolver, mock_git_manager):
        """Create merge registry with mocked dependencies."""
        from gobby.mcp_proxy.tools.merge import create_merge_registry

        return create_merge_registry(
            merge_storage=mock_storage,
            merge_resolver=mock_resolver,
            git_manager=mock_git_manager,
        )

    @pytest.mark.asyncio
    async def test_merge_abort_cancels_merge(self, merge_registry, mock_storage, mock_git_manager):
        """merge_abort cancels the merge and restores state."""
        from gobby.storage.merge_resolutions import MergeResolution

        mock_resolution = MergeResolution(
            id="mr-test123",
            worktree_id="wt-abc",
            source_branch="feature/test",
            target_branch="main",
            status="pending",
            tier_used=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.get_resolution.return_value = mock_resolution
        mock_storage.delete_resolution.return_value = True

        result = await merge_registry.call("merge_abort", {"resolution_id": "mr-test123"})

        assert result["success"] is True
        assert "aborted" in result["message"].lower()
        mock_storage.delete_resolution.assert_called_once_with("mr-test123")

    @pytest.mark.asyncio
    async def test_merge_abort_resolution_not_found(self, merge_registry, mock_storage):
        """merge_abort returns error for unknown resolution."""
        mock_storage.get_resolution.return_value = None

        result = await merge_registry.call("merge_abort", {"resolution_id": "mr-unknown"})

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_merge_abort_already_resolved(self, merge_registry, mock_storage):
        """merge_abort fails for already resolved merge."""
        from gobby.storage.merge_resolutions import MergeResolution

        mock_resolution = MergeResolution(
            id="mr-test123",
            worktree_id="wt-abc",
            source_branch="feature/test",
            target_branch="main",
            status="resolved",
            tier_used="git_auto",
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.get_resolution.return_value = mock_resolution

        result = await merge_registry.call("merge_abort", {"resolution_id": "mr-test123"})

        assert result["success"] is False
        assert "already" in result["error"].lower() or "resolved" in result["error"].lower()


# ==============================================================================
# Argument Validation Tests
# ==============================================================================


class TestMergeToolValidation:
    """Tests for argument validation across merge tools."""

    @pytest.fixture
    def mock_storage(self):
        """Create mock merge resolution storage."""
        storage = MagicMock()
        storage.get_resolution = MagicMock(return_value=None)
        storage.get_conflict = MagicMock(return_value=None)
        return storage

    @pytest.fixture
    def mock_resolver(self):
        """Create mock merge resolver."""
        return MagicMock()

    @pytest.fixture
    def mock_git_manager(self):
        """Create mock git manager."""
        return MagicMock()

    @pytest.fixture
    def merge_registry(self, mock_storage, mock_resolver, mock_git_manager):
        """Create merge registry with mocked dependencies."""
        from gobby.mcp_proxy.tools.merge import create_merge_registry

        return create_merge_registry(
            merge_storage=mock_storage,
            merge_resolver=mock_resolver,
            git_manager=mock_git_manager,
        )

    @pytest.mark.asyncio
    async def test_merge_start_validates_branch_names(self, merge_registry):
        """merge_start validates branch name format."""
        result = await merge_registry.call(
            "merge_start",
            {
                "worktree_id": "wt-abc",
                "source_branch": "",
                "target_branch": "main",
            },
        )

        assert result["success"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_merge_status_validates_resolution_id_format(self, merge_registry):
        """merge_status validates resolution ID format."""
        result = await merge_registry.call("merge_status", {"resolution_id": ""})

        assert result["success"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_merge_resolve_validates_conflict_id_format(self, merge_registry):
        """merge_resolve validates conflict ID format."""
        result = await merge_registry.call("merge_resolve", {"conflict_id": ""})

        assert result["success"] is False
        assert "error" in result


# ==============================================================================
# Error Response Tests
# ==============================================================================


class TestMergeToolErrors:
    """Tests for error handling in merge tools."""

    @pytest.fixture
    def mock_storage(self):
        """Create mock merge resolution storage that raises errors."""
        storage = MagicMock()
        return storage

    @pytest.fixture
    def mock_resolver(self):
        """Create mock merge resolver."""
        resolver = MagicMock()
        resolver.resolve = AsyncMock()
        return resolver

    @pytest.fixture
    def mock_git_manager(self):
        """Create mock git manager."""
        return MagicMock()

    @pytest.fixture
    def merge_registry(self, mock_storage, mock_resolver, mock_git_manager):
        """Create merge registry with mocked dependencies."""
        from gobby.mcp_proxy.tools.merge import create_merge_registry

        return create_merge_registry(
            merge_storage=mock_storage,
            merge_resolver=mock_resolver,
            git_manager=mock_git_manager,
        )

    @pytest.mark.asyncio
    async def test_merge_start_handles_storage_error(
        self, merge_registry, mock_storage, mock_resolver
    ):
        """merge_start handles storage errors gracefully."""
        mock_storage.create_resolution.side_effect = Exception("Database error")

        result = await merge_registry.call(
            "merge_start",
            {
                "worktree_id": "wt-abc",
                "source_branch": "feature/test",
                "target_branch": "main",
            },
        )

        assert result["success"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_merge_resolve_handles_resolver_error(
        self, merge_registry, mock_storage, mock_resolver
    ):
        """merge_resolve handles resolver errors gracefully."""
        from gobby.storage.merge_resolutions import MergeConflict

        mock_conflict = MergeConflict(
            id="mc-conflict1",
            resolution_id="mr-test123",
            file_path="src/test.py",
            status="pending",
            ours_content="our version",
            theirs_content="their version",
            resolved_content=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.get_conflict.return_value = mock_conflict
        mock_resolver.resolve_file = AsyncMock(side_effect=Exception("AI error"))

        result = await merge_registry.call("merge_resolve", {"conflict_id": "mc-conflict1"})

        assert result["success"] is False
        assert "error" in result
