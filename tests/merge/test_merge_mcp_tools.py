"""Tests for gobby-merge MCP server tools (TDD green phase).

Tests for MCP tools in gobby-merge server:
- merge_start: Initiate merge with AI resolution
- merge_status: Get current merge state and conflicts
- merge_resolve: Apply AI resolution to specific conflict
- merge_apply: Apply all resolutions and complete merge
- merge_abort: Cancel merge and restore state
"""

import asyncio
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock

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
        from gobby.mcp_proxy.tools import merge

        assert hasattr(merge, "create_merge_registry")

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
        storage.get_resolution_for_merge = MagicMock(return_value=None)
        storage.get_or_create_resolution = MagicMock()
        storage.get_active_resolution = MagicMock(return_value=None)
        storage.get_resolution = MagicMock()
        storage.update_resolution = MagicMock()
        storage.create_conflict = MagicMock()
        storage.list_conflicts = MagicMock(return_value=[])
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
        worktree_manager.get.return_value = mock_worktree
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
        mock_storage.get_or_create_resolution.return_value = (mock_resolution, True)

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
        mock_storage.get_or_create_resolution.assert_called_once()

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
        mock_storage.get_or_create_resolution.return_value = (mock_resolution, True)

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
    async def test_merge_start_reuses_resolved_resolution(
        self, merge_registry, mock_storage, mock_resolver
    ):
        """merge_start reuses an exact resolved resolution."""
        from gobby.storage.merge_resolutions import MergeResolution

        existing = MergeResolution(
            id="mr-test123",
            worktree_id="wt-abc",
            source_branch="feature/test",
            target_branch="main",
            status="resolved",
            tier_used="git_auto",
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.get_resolution_for_merge.return_value = existing

        result = await merge_registry.call(
            "merge_start",
            {
                "worktree_id": "wt-abc",
                "source_branch": "feature/test",
                "target_branch": "main",
            },
        )

        assert result["success"] is True
        assert result["resolution_id"] == "mr-test123"
        assert result["reused_resolution"] is True
        mock_storage.get_or_create_resolution.assert_not_called()
        mock_resolver.resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_merge_start_reuses_pending_conflicts(
        self, merge_registry, mock_storage, mock_resolver
    ):
        """merge_start returns existing conflicts for an exact pending resolution."""
        from gobby.storage.merge_resolutions import MergeConflict, MergeResolution

        existing = MergeResolution(
            id="mr-test123",
            worktree_id="wt-abc",
            source_branch="feature/test",
            target_branch="main",
            status="pending",
            tier_used=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        conflict = MergeConflict(
            id="mc-test123",
            resolution_id="mr-test123",
            file_path="src/test.py",
            status="pending",
            ours_content="ours",
            theirs_content="theirs",
            resolved_content=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.get_resolution_for_merge.return_value = existing
        mock_storage.list_conflicts.return_value = [conflict]

        result = await merge_registry.call(
            "merge_start",
            {
                "worktree_id": "wt-abc",
                "source_branch": "feature/test",
                "target_branch": "main",
            },
        )

        assert result["success"] is False
        assert result["resolution_id"] == "mr-test123"
        assert result["conflicts"] == [{"file": "src/test.py"}]
        assert result["reused_resolution"] is True
        mock_storage.get_or_create_resolution.assert_not_called()
        mock_storage.create_conflict.assert_not_called()
        mock_resolver.resolve.assert_not_called()

    @pytest.mark.asyncio
    async def test_merge_start_refreshes_empty_pending_resolution(
        self, merge_registry, mock_storage, mock_resolver
    ):
        """merge_start reruns resolution for exact pending rows without conflicts."""
        from gobby.storage.merge_resolutions import MergeResolution
        from gobby.worktrees.merge import MergeResult, ResolutionTier

        existing = MergeResolution(
            id="mr-test123",
            worktree_id="wt-abc",
            source_branch="feature/test",
            target_branch="main",
            status="pending",
            tier_used=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.get_resolution_for_merge.return_value = existing
        mock_resolver.resolve.return_value = MergeResult(
            success=True,
            tier=ResolutionTier.GIT_AUTO,
            conflicts=[],
            resolved_files=[],
            unresolved_conflicts=[],
            needs_human_review=False,
        )

        result = await merge_registry.call(
            "merge_start",
            {
                "worktree_id": "wt-abc",
                "source_branch": "feature/test",
                "target_branch": "main",
            },
        )

        assert result["success"] is True
        assert result["resolution_id"] == "mr-test123"
        mock_storage.get_or_create_resolution.assert_not_called()
        mock_resolver.resolve.assert_called_once()

    @pytest.mark.asyncio
    async def test_merge_start_rejects_different_active_pending_resolution(
        self, merge_registry, mock_storage, mock_resolver
    ):
        """merge_start returns a clean error when another merge is pending."""
        from gobby.storage.merge_resolutions import MergeResolution

        active = MergeResolution(
            id="mr-active",
            worktree_id="wt-abc",
            source_branch="feature/other",
            target_branch="develop",
            status="pending",
            tier_used=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.get_active_resolution.return_value = active

        result = await merge_registry.call(
            "merge_start",
            {
                "worktree_id": "wt-abc",
                "source_branch": "feature/test",
                "target_branch": "main",
            },
        )

        assert result["success"] is False
        assert result["resolution_id"] == "mr-active"
        assert "active merge resolution" in result["error"].lower()
        mock_storage.get_or_create_resolution.assert_not_called()
        mock_resolver.resolve.assert_not_called()

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
        """merge_status includes compact conflict details by default."""
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
        assert result["conflicts"][0]["has_ours_content"] is True
        assert result["conflicts"][0]["has_theirs_content"] is True
        assert result["conflicts"][0]["has_resolved_content"] is False
        assert "ours_content" not in result["conflicts"][0]
        assert "theirs_content" not in result["conflicts"][0]
        assert "resolved_content" not in result["conflicts"][0]

    @pytest.mark.asyncio
    async def test_merge_status_can_include_conflict_content(self, merge_registry, mock_storage):
        """merge_status can opt into full conflict content for debug/manual callers."""
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
        mock_storage.list_conflicts.return_value = [
            MergeConflict(
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
        ]

        result = await merge_registry.call(
            "merge_status",
            {"resolution_id": "mr-test123", "include_content": True},
        )

        assert result["success"] is True
        assert result["conflicts"][0]["ours_content"] == "our version"
        assert result["conflicts"][0]["theirs_content"] == "their version"
        assert result["conflicts"][0]["resolved_content"] == "merged version"

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
    async def test_merge_resolve_returns_ai_failure_reason(
        self, mock_storage, mock_resolver, mock_git_manager
    ):
        """merge_resolve surfaces resolver failure detail to workers."""
        from gobby.mcp_proxy.tools.merge import create_merge_registry
        from gobby.storage.merge_resolutions import MergeConflict
        from gobby.worktrees.merge import ResolutionResult, ResolutionTier

        mock_storage.get_conflict.return_value = MergeConflict(
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
        mock_storage.get_resolution.return_value = None
        mock_resolver.resolve_file.return_value = ResolutionResult(
            success=False,
            tier=ResolutionTier.HUMAN_REVIEW,
            conflicts=[],
            resolved_files=[],
            unresolved_conflicts=[],
            needs_human_review=True,
            failure_reason="hunk_count_mismatch:src/test.py:file_blocks=2:ai_hunks=1",
        )
        registry = create_merge_registry(
            merge_storage=mock_storage,
            merge_resolver=mock_resolver,
            git_manager=mock_git_manager,
        )

        result = await registry.call("merge_resolve", {"conflict_id": "mc-conflict1"})

        assert result["success"] is False
        assert result["error"] == "AI resolution failed"
        assert result["failure_reason"] == (
            "hunk_count_mismatch:src/test.py:file_blocks=2:ai_hunks=1"
        )

    @pytest.mark.asyncio
    async def test_merge_resolve_rejects_parallel_ai_resolves_for_same_resolution(
        self, mock_storage, mock_resolver, mock_git_manager
    ):
        """merge_resolve fails fast instead of parallelizing one active resolution."""
        from gobby.mcp_proxy.tools.merge import create_merge_registry
        from gobby.storage.merge_resolutions import MergeConflict
        from gobby.worktrees.merge import ResolutionResult, ResolutionTier

        first_started = asyncio.Event()
        release_first = asyncio.Event()

        conflicts = {
            "mc-conflict1": MergeConflict(
                id="mc-conflict1",
                resolution_id="mr-test123",
                file_path="src/one.py",
                status="pending",
                ours_content="our version",
                theirs_content="their version",
                resolved_content=None,
                created_at="2025-01-01T00:00:00+00:00",
                updated_at="2025-01-01T00:00:00+00:00",
            ),
            "mc-conflict2": MergeConflict(
                id="mc-conflict2",
                resolution_id="mr-test123",
                file_path="src/two.py",
                status="pending",
                ours_content="our version",
                theirs_content="their version",
                resolved_content=None,
                created_at="2025-01-01T00:00:00+00:00",
                updated_at="2025-01-01T00:00:00+00:00",
            ),
        }

        mock_storage.get_conflict.side_effect = lambda conflict_id: conflicts.get(conflict_id)
        mock_storage.get_resolution.return_value = None

        async def resolve_file(**kwargs):
            first_started.set()
            await release_first.wait()
            return ResolutionResult(
                success=True,
                tier=ResolutionTier.CONFLICT_ONLY_AI,
                conflicts=[],
                resolved_files=[kwargs["path"]],
                unresolved_conflicts=[],
                needs_human_review=False,
                resolved_content_by_file={kwargs["path"]: "merged version"},
            )

        mock_resolver.resolve_file.side_effect = resolve_file
        mock_storage.update_conflict.side_effect = lambda conflict_id, **kwargs: MergeConflict(
            id=conflict_id,
            resolution_id="mr-test123",
            file_path=conflicts[conflict_id].file_path,
            status=kwargs["status"],
            ours_content="our version",
            theirs_content="their version",
            resolved_content=kwargs["resolved_content"],
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        registry = create_merge_registry(
            merge_storage=mock_storage,
            merge_resolver=mock_resolver,
            git_manager=mock_git_manager,
        )

        first = asyncio.create_task(registry.call("merge_resolve", {"conflict_id": "mc-conflict1"}))
        await first_started.wait()

        second = await registry.call("merge_resolve", {"conflict_id": "mc-conflict2"})
        release_first.set()
        first_result = await first

        assert first_result["success"] is True
        assert second["success"] is False
        assert second["retry_later"] is True
        assert second["resolution_id"] == "mr-test123"
        assert "do not parallelize" in second["error"]
        mock_resolver.resolve_file.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_merge_resolve_reads_current_conflict_hunks_from_worktree(
        self, tmp_path, mock_storage, mock_resolver, mock_git_manager
    ):
        """merge_resolve prefers current on-disk conflict markers over stale row text."""
        from gobby.mcp_proxy.tools.merge import create_merge_registry
        from gobby.storage.merge_resolutions import MergeConflict, MergeResolution
        from gobby.worktrees.merge import ResolutionResult, ResolutionTier

        worktree_path = tmp_path / "wt"
        conflict_path = worktree_path / "src/test.py"
        conflict_path.parent.mkdir(parents=True)
        conflict_path.write_text(
            "<<<<<<< HEAD\nours one\n=======\ntheirs one\n>>>>>>> main\n"
            "keep\n"
            "<<<<<<< HEAD\nours two\n=======\ntheirs two\n>>>>>>> main\n",
            encoding="utf-8",
        )
        worktree = MagicMock(worktree_path=str(worktree_path))
        worktree_manager = MagicMock()
        worktree_manager.get.return_value = worktree
        mock_storage.get_resolution.return_value = MergeResolution(
            id="mr-test123",
            worktree_id="wt-abc",
            source_branch="feature/test",
            target_branch="main",
            status="pending",
            tier_used=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_storage.get_conflict.return_value = MergeConflict(
            id="mc-conflict1",
            resolution_id="mr-test123",
            file_path="src/test.py",
            status="pending",
            ours_content="stale ours",
            theirs_content="stale theirs",
            resolved_content=None,
            created_at="2025-01-01T00:00:00+00:00",
            updated_at="2025-01-01T00:00:00+00:00",
        )
        mock_resolver.resolve_file.return_value = ResolutionResult(
            success=True,
            tier=ResolutionTier.CONFLICT_ONLY_AI,
            conflicts=[],
            resolved_files=["src/test.py"],
            unresolved_conflicts=[],
            needs_human_review=False,
            resolved_content_by_file={"src/test.py": "merged version"},
        )
        resolved_conflict = mock_storage.get_conflict.return_value
        resolved_conflict.status = "resolved"
        resolved_conflict.resolved_content = "merged version"
        mock_storage.update_conflict.return_value = resolved_conflict
        registry = create_merge_registry(
            merge_storage=mock_storage,
            merge_resolver=mock_resolver,
            git_manager=mock_git_manager,
            worktree_manager=worktree_manager,
        )

        result = await registry.call("merge_resolve", {"conflict_id": "mc-conflict1"})

        assert result["success"] is True
        hunks = mock_resolver.resolve_file.call_args.kwargs["conflict_hunks"]
        assert len(hunks) == 2
        assert hunks[0].ours == "ours one"
        assert hunks[1].theirs == "theirs two"

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
        """Create mock git manager with subprocess-like public git methods."""
        git_manager = MagicMock()
        git_manager.stage_files = MagicMock()
        git_manager.get_unmerged_files = MagicMock(return_value=[])
        git_manager.run_git_command = MagicMock()
        return git_manager

    @pytest.fixture
    def mock_worktree_manager(self, tmp_path: Path) -> MagicMock:
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
            if args == ["rev-parse", "-q", "--verify", "MERGE_HEAD"]:
                result.stdout = "merge-head\n"
            elif args == ["rev-parse", "HEAD"]:
                result.stdout = "merged-sha\n"
            else:
                result.stdout = ""
            return result

        mock_git_manager.stage_files.side_effect = lambda *args, **kwargs: fake_run_git(
            ["add", "--"], **kwargs
        )
        mock_git_manager.run_git_command.side_effect = fake_run_git

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
        mock_git_manager.stage_files.assert_called_once_with(["src/test.py"], cwd=str(tmp_path))
        mock_git_manager.get_unmerged_files.assert_called_once_with(cwd=str(tmp_path))
        mock_git_manager.run_git_command.assert_any_call(
            ["rev-parse", "-q", "--verify", "MERGE_HEAD"], cwd=str(tmp_path), timeout=10
        )
        mock_git_manager.run_git_command.assert_any_call(
            ["commit", "--no-edit"], cwd=str(tmp_path), timeout=30
        )
        mock_git_manager.run_git_command.assert_any_call(
            ["rev-parse", "HEAD"], cwd=str(tmp_path), timeout=10
        )
        assert result["merge_sha"] == "merged-sha"

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
        git_manager.run_git_command = MagicMock()
        return git_manager

    @pytest.fixture
    def mock_worktree_manager(self, tmp_path: Path) -> MagicMock:
        """Create mock worktree manager."""
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

        def fake_run_git(args, cwd=None, timeout=None):
            result = MagicMock()
            result.returncode = 0
            result.stdout = (
                "merge-head\n" if args == ["rev-parse", "-q", "--verify", "MERGE_HEAD"] else ""
            )
            result.stderr = ""
            return result

        mock_git_manager.run_git_command.side_effect = fake_run_git

        result = await merge_registry.call("merge_abort", {"resolution_id": "mr-test123"})

        assert result["success"] is True
        assert "aborted" in result["message"].lower()
        mock_git_manager.run_git_command.assert_any_call(["merge", "--abort"], cwd=ANY, timeout=30)
        mock_storage.delete_resolution.assert_called_once_with("mr-test123")

    @pytest.mark.asyncio
    async def test_merge_abort_failure_preserves_resolution(
        self, merge_registry, mock_storage, mock_git_manager
    ):
        """merge_abort keeps storage when git merge --abort fails."""
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

        def fake_run_git(args, cwd=None, timeout=None):
            result = MagicMock()
            if args == ["merge", "--abort"]:
                result.returncode = 1
                result.stderr = "abort failed"
                result.stdout = ""
            else:
                result.returncode = 0
                result.stdout = "merge-head\n"
                result.stderr = ""
            return result

        mock_git_manager.run_git_command.side_effect = fake_run_git

        result = await merge_registry.call("merge_abort", {"resolution_id": "mr-test123"})

        assert result["success"] is False
        assert "git merge --abort failed" in result["error"]
        mock_storage.delete_resolution.assert_not_called()

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
        storage.get_resolution_for_merge = MagicMock(return_value=None)
        storage.get_active_resolution = MagicMock(return_value=None)
        storage.get_or_create_resolution = MagicMock()
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
        mock_storage.get_or_create_resolution.side_effect = Exception("Database error")

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
