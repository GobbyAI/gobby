"""Tests for MergeResolver tiered strategy (TDD Red Phase).

Tests for MergeResolver class implementing tiered resolution:
- Tier 1: Git auto-merge succeeds (no conflicts)
- Tier 2: Conflict-only AI resolution (sends only conflict hunks to LLM)
- Tier 3: Full-file AI resolution (sends entire file for complex conflicts)
- Tier 4: Human review fallback (marks as needs-human-review)
"""

from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.unit


class _StaticProvider:
    def __init__(self, response: str):
        self.response = response

    async def generate_text(self, *_args, **_kwargs) -> str:
        return self.response


class _StaticLLMService:
    def __init__(self, response: str):
        self.provider = _StaticProvider(response)

    def get_default_provider(self) -> _StaticProvider:
        return self.provider


# =============================================================================
# Import Tests
# =============================================================================


class TestMergeResolverImport:
    """Tests for MergeResolver import."""

    def test_import_merge_resolver(self) -> None:
        """Test that MergeResolver can be imported."""
        from gobby.worktrees.merge.resolver import MergeResolver

        assert MergeResolver is not None

    def test_import_merge_result(self) -> None:
        """Test that MergeResult can be imported."""
        from gobby.worktrees.merge.resolver import MergeResult

        assert MergeResult is not None

    def test_import_resolution_tier(self) -> None:
        """Test that ResolutionTier enum can be imported."""
        from gobby.worktrees.merge.resolver import ResolutionTier

        assert ResolutionTier is not None
        assert hasattr(ResolutionTier, "GIT_AUTO")
        assert hasattr(ResolutionTier, "CONFLICT_ONLY_AI")
        assert hasattr(ResolutionTier, "FULL_FILE_AI")
        assert hasattr(ResolutionTier, "HUMAN_REVIEW")

    def test_splice_accepts_separator_with_trailing_spaces(self) -> None:
        from gobby.worktrees.merge.resolver import splice_resolutions_into_file

        content = "<<<<<<< HEAD\nours\n=======   \ntheirs\n>>>>>>> feature\n"

        assert splice_resolutions_into_file(content, ["resolved"]) == "resolved\n"


# =============================================================================
# Tier 1: Git Auto-Merge Tests
# =============================================================================


class TestTier1GitAutoMerge:
    """Tests for Tier 1: Git auto-merge (no conflicts)."""

    @pytest.mark.asyncio
    async def test_git_auto_merge_succeeds(self):
        """Test successful git auto-merge with no conflicts."""
        from gobby.worktrees.merge.resolver import MergeResolver, ResolutionTier

        resolver = MergeResolver()

        # Mock git merge returning success
        with patch.object(resolver, "_git_merge", new_callable=AsyncMock) as mock_git:
            mock_git.return_value = {"success": True, "conflicts": []}

            result = await resolver.resolve(
                worktree_path="/path/to/worktree",
                source_branch="feature/test",
                target_branch="main",
            )

            assert result.success is True
            assert result.tier == ResolutionTier.GIT_AUTO
            assert len(result.conflicts) == 0

    @pytest.mark.asyncio
    async def test_git_auto_merge_skips_ai_when_no_conflicts(self):
        """Test that AI is not called when git auto-merge succeeds."""
        from gobby.worktrees.merge.resolver import MergeResolver

        resolver = MergeResolver()

        with patch.object(resolver, "_git_merge", new_callable=AsyncMock) as mock_git:
            with patch.object(
                resolver, "_resolve_conflicts_only", new_callable=AsyncMock
            ) as mock_ai:
                mock_git.return_value = {"success": True, "conflicts": []}

                await resolver.resolve(
                    worktree_path="/path/to/worktree",
                    source_branch="feature/test",
                    target_branch="main",
                )

                mock_ai.assert_not_called()
                assert mock_ai.call_count == 0
                assert not mock_ai.called


# =============================================================================
# Tier 2: Conflict-Only AI Resolution Tests
# =============================================================================


class TestTier2ConflictOnlyAI:
    """Tests for Tier 2: Conflict-only AI resolution."""

    @pytest.mark.asyncio
    async def test_conflict_only_ai_sends_hunks_not_full_file(self):
        """Test that Tier 2 sends only conflict hunks to LLM."""
        from gobby.worktrees.merge.resolver import MergeResolver

        resolver = MergeResolver()

        with patch.object(resolver, "_git_merge", new_callable=AsyncMock) as mock_git:
            with patch.object(
                resolver, "_resolve_conflicts_only", new_callable=AsyncMock
            ) as mock_ai:
                mock_git.return_value = {
                    "success": False,
                    "conflicts": [{"file": "test.py", "hunks": ["conflict1"]}],
                }
                mock_ai.return_value = {"success": True, "resolutions": []}

                await resolver.resolve(
                    worktree_path="/path/to/worktree",
                    source_branch="feature/test",
                    target_branch="main",
                )

                # Should have called conflict-only resolution
                mock_ai.assert_called_once()
                assert mock_ai.call_count == 1
                assert mock_ai.call_args is not None

    @pytest.mark.asyncio
    async def test_conflict_only_ai_resolves_simple_conflicts(self):
        """Test that Tier 2 can resolve simple conflicts."""
        from gobby.worktrees.merge.resolver import MergeResolver, ResolutionTier

        resolver = MergeResolver()

        with patch.object(resolver, "_git_merge", new_callable=AsyncMock) as mock_git:
            with patch.object(
                resolver, "_resolve_conflicts_only", new_callable=AsyncMock
            ) as mock_ai:
                mock_git.return_value = {
                    "success": False,
                    "conflicts": [{"file": "test.py", "hunks": ["conflict"]}],
                }
                mock_ai.return_value = {"success": True, "resolutions": ["merged"]}

                result = await resolver.resolve(
                    worktree_path="/path/to/worktree",
                    source_branch="feature/test",
                    target_branch="main",
                )

                assert result.success is True
                assert result.tier == ResolutionTier.CONFLICT_ONLY_AI

    @pytest.mark.asyncio
    async def test_conflict_only_ai_extracts_single_fenced_hunk(self, tmp_path):
        """Tier 2 accepts one fenced code block as the hunk body."""
        from gobby.worktrees.merge.resolver import MergeResolver

        conflict_path = tmp_path / "test.py"
        conflict_path.write_text(
            "before\n<<<<<<< HEAD\nours()\n=======\ntheirs()\n>>>>>>> main\nafter\n",
            encoding="utf-8",
        )
        resolver = MergeResolver(llm_service=_StaticLLMService("```python\nresolved()\n```"))

        result = await resolver._resolve_conflicts_only(
            [
                {
                    "file": "test.py",
                    "worktree_path": str(tmp_path),
                    "hunks": [{"ours": "ours()", "theirs": "theirs()"}],
                }
            ]
        )

        assert result["success"] is True
        assert result["resolutions"][0]["content"] == "before\nresolved()\nafter\n"

    @pytest.mark.asyncio
    async def test_conflict_only_ai_rejects_prose_fenced_hunk(self, tmp_path):
        """Tier 2 rejects explanations instead of splicing them into source."""
        from gobby.worktrees.merge.resolver import MergeResolver

        conflict_path = tmp_path / "test.py"
        conflict_path.write_text(
            "<<<<<<< HEAD\nours()\n=======\ntheirs()\n>>>>>>> main\n",
            encoding="utf-8",
        )
        response = "Here are the resolved hunks:\n```python\nresolved()\n```\n\nRationale: ok"
        resolver = MergeResolver(llm_service=_StaticLLMService(response))

        result = await resolver._resolve_conflicts_only(
            [
                {
                    "file": "test.py",
                    "worktree_path": str(tmp_path),
                    "hunks": [{"ours": "ours()", "theirs": "theirs()"}],
                }
            ]
        )

        assert result == {"success": False, "resolutions": []}


# =============================================================================
# Tier 3: Full-File AI Resolution Tests
# =============================================================================


class TestTier3FullFileAI:
    """Tests for Tier 3: Full-file AI resolution."""

    @pytest.mark.asyncio
    async def test_full_file_ai_escalates_from_tier2(self):
        """Test that Tier 3 is used when Tier 2 fails."""
        from gobby.worktrees.merge.resolver import MergeResolver, ResolutionTier

        resolver = MergeResolver()

        with patch.object(resolver, "_git_merge", new_callable=AsyncMock) as mock_git:
            with patch.object(
                resolver, "_resolve_conflicts_only", new_callable=AsyncMock
            ) as mock_t2:
                with patch.object(
                    resolver, "_resolve_full_file", new_callable=AsyncMock
                ) as mock_t3:
                    mock_git.return_value = {
                        "success": False,
                        "conflicts": [{"file": "test.py", "complex": True}],
                    }
                    mock_t2.return_value = {"success": False, "reason": "too_complex"}
                    mock_t3.return_value = {"success": True, "resolutions": ["merged"]}

                    result = await resolver.resolve(
                        worktree_path="/path/to/worktree",
                        source_branch="feature/test",
                        target_branch="main",
                    )

                    assert result.tier == ResolutionTier.FULL_FILE_AI

    @pytest.mark.asyncio
    async def test_full_file_ai_sends_entire_file(self):
        """Test that Tier 3 sends entire file content to LLM."""
        from gobby.worktrees.merge.resolver import MergeResolver

        resolver = MergeResolver()

        with patch.object(resolver, "_git_merge", new_callable=AsyncMock) as mock_git:
            with patch.object(
                resolver, "_resolve_conflicts_only", new_callable=AsyncMock
            ) as mock_t2:
                with patch.object(
                    resolver, "_resolve_full_file", new_callable=AsyncMock
                ) as mock_t3:
                    mock_git.return_value = {
                        "success": False,
                        "conflicts": [{"file": "test.py"}],
                    }
                    mock_t2.return_value = {"success": False}
                    mock_t3.return_value = {"success": True}

                    await resolver.resolve(
                        worktree_path="/path/to/worktree",
                        source_branch="feature/test",
                        target_branch="main",
                    )

                    mock_t3.assert_called_once()
                    assert mock_t3.call_count == 1
                    assert mock_t3.call_args is not None

    @pytest.mark.asyncio
    async def test_full_file_ai_extracts_single_fenced_file(self, tmp_path):
        """Tier 3 accepts one fenced full-file response and stores only source."""
        from gobby.worktrees.merge.resolver import MergeResolver

        conflict_path = tmp_path / "test.py"
        conflict_path.write_text(
            "<<<<<<< HEAD\nours()\n=======\ntheirs()\n>>>>>>> main\n",
            encoding="utf-8",
        )
        resolver = MergeResolver(llm_service=_StaticLLMService("```python\nresolved()\n```"))

        result = await resolver._resolve_full_file(
            [{"file": "test.py", "worktree_path": str(tmp_path)}]
        )

        assert result["success"] is True
        assert result["resolutions"][0]["content"] == "resolved()"

    @pytest.mark.asyncio
    async def test_full_file_ai_rejects_prose_fenced_file(self, tmp_path):
        """Tier 3 rejects markdown explanations rather than storing them."""
        from gobby.worktrees.merge.resolver import MergeResolver

        conflict_path = tmp_path / "test.py"
        conflict_path.write_text(
            "<<<<<<< HEAD\nours()\n=======\ntheirs()\n>>>>>>> main\n",
            encoding="utf-8",
        )
        response = "Here is the resolved file:\n```python\nresolved()\n```\n\nRationale: ok"
        resolver = MergeResolver(llm_service=_StaticLLMService(response))

        result = await resolver._resolve_full_file(
            [{"file": "test.py", "worktree_path": str(tmp_path)}]
        )

        assert result == {"success": False, "resolutions": []}


# =============================================================================
# Tier 4: Human Review Fallback Tests
# =============================================================================


class TestTier4HumanReview:
    """Tests for Tier 4: Human review fallback."""

    @pytest.mark.asyncio
    async def test_human_review_when_all_tiers_fail(self):
        """Test human review fallback when all AI tiers fail."""
        from gobby.worktrees.merge.resolver import MergeResolver, ResolutionTier

        resolver = MergeResolver()

        with patch.object(resolver, "_git_merge", new_callable=AsyncMock) as mock_git:
            with patch.object(
                resolver, "_resolve_conflicts_only", new_callable=AsyncMock
            ) as mock_t2:
                with patch.object(
                    resolver, "_resolve_full_file", new_callable=AsyncMock
                ) as mock_t3:
                    mock_git.return_value = {"success": False, "conflicts": [{}]}
                    mock_t2.return_value = {"success": False}
                    mock_t3.return_value = {"success": False}

                    result = await resolver.resolve(
                        worktree_path="/path/to/worktree",
                        source_branch="feature/test",
                        target_branch="main",
                    )

                    assert result.success is False
                    assert result.tier == ResolutionTier.HUMAN_REVIEW
                    assert result.needs_human_review is True

    @pytest.mark.asyncio
    async def test_human_review_provides_conflict_details(self):
        """Test that human review includes conflict details for manual resolution."""
        from gobby.worktrees.merge.resolver import MergeResolver

        resolver = MergeResolver()

        with patch.object(resolver, "_git_merge", new_callable=AsyncMock) as mock_git:
            with patch.object(
                resolver, "_resolve_conflicts_only", new_callable=AsyncMock
            ) as mock_t2:
                with patch.object(
                    resolver, "_resolve_full_file", new_callable=AsyncMock
                ) as mock_t3:
                    mock_git.return_value = {
                        "success": False,
                        "conflicts": [
                            {"file": "a.py", "hunks": ["conflict1"]},
                            {"file": "b.py", "hunks": ["conflict2"]},
                        ],
                    }
                    mock_t2.return_value = {"success": False}
                    mock_t3.return_value = {"success": False}

                    result = await resolver.resolve(
                        worktree_path="/path/to/worktree",
                        source_branch="feature/test",
                        target_branch="main",
                    )

                    assert len(result.unresolved_conflicts) == 2


# =============================================================================
# Parallel Resolution Tests
# =============================================================================


class TestParallelResolution:
    """Tests for parallel resolution of multiple files."""

    @pytest.mark.asyncio
    async def test_parallel_resolution_multiple_files(self):
        """Test that multiple conflicting files are resolved in parallel."""
        from gobby.worktrees.merge.resolver import MergeResolver

        resolver = MergeResolver()

        with patch.object(
            resolver, "_resolve_file_conflict", new_callable=AsyncMock
        ) as mock_resolve:
            mock_resolve.return_value = {"success": True}

            conflicts = [
                {"file": "a.py"},
                {"file": "b.py"},
                {"file": "c.py"},
            ]

            resolved, unresolved = await resolver.resolve_conflicts_parallel(
                worktree_path="/path/to/worktree",
                conflicts=conflicts,
            )

            # Should have resolved all three files
            assert mock_resolve.call_count == 3
            assert len(resolved) == 3
            assert len(unresolved) == 0

    @pytest.mark.asyncio
    async def test_parallel_resolution_handles_partial_failure(self):
        """Test handling when some files resolve and others don't."""
        from gobby.worktrees.merge.resolver import MergeResolver

        resolver = MergeResolver()

        with patch.object(
            resolver, "_resolve_file_conflict", new_callable=AsyncMock
        ) as mock_resolve:
            # First file resolves, second doesn't
            mock_resolve.side_effect = [
                {"success": True},
                {"success": False},
            ]

            conflicts = [
                {"file": "a.py"},
                {"file": "b.py"},
            ]

            resolved, unresolved = await resolver.resolve_conflicts_parallel(
                worktree_path="/path/to/worktree",
                conflicts=conflicts,
            )

            # Should track partial success
            assert len(resolved) == 1
            assert len(unresolved) == 1


# =============================================================================
# Strategy Escalation Tests
# =============================================================================


class TestStrategyEscalation:
    """Tests for strategy escalation when lower tiers fail."""

    @pytest.mark.asyncio
    async def test_escalation_order(self):
        """Test that tiers are tried in correct order."""
        from gobby.worktrees.merge.resolver import MergeResolver

        resolver = MergeResolver()
        call_order = []

        async def track_git(*args, **kwargs):
            call_order.append("git")
            return {"success": False, "conflicts": [{"file": "test.py"}]}

        async def track_t2(*args, **kwargs):
            call_order.append("tier2")
            return {"success": False}

        async def track_t3(*args, **kwargs):
            call_order.append("tier3")
            return {"success": True}

        with patch.object(resolver, "_git_merge", side_effect=track_git):
            with patch.object(resolver, "_resolve_conflicts_only", side_effect=track_t2):
                with patch.object(resolver, "_resolve_full_file", side_effect=track_t3):
                    await resolver.resolve(
                        worktree_path="/path/to/worktree",
                        source_branch="feature/test",
                        target_branch="main",
                    )

        assert call_order == ["git", "tier2", "tier3"]

    @pytest.mark.asyncio
    async def test_escalation_stops_on_success(self):
        """Test that escalation stops when a tier succeeds."""
        from gobby.worktrees.merge.resolver import MergeResolver, ResolutionTier

        resolver = MergeResolver()

        with patch.object(resolver, "_git_merge", new_callable=AsyncMock) as mock_git:
            with patch.object(
                resolver, "_resolve_conflicts_only", new_callable=AsyncMock
            ) as mock_t2:
                with patch.object(
                    resolver, "_resolve_full_file", new_callable=AsyncMock
                ) as mock_t3:
                    mock_git.return_value = {"success": False, "conflicts": [{}]}
                    mock_t2.return_value = {"success": True}

                    result = await resolver.resolve(
                        worktree_path="/path/to/worktree",
                        source_branch="feature/test",
                        target_branch="main",
                    )

                    # Tier 2 succeeded, Tier 3 should not be called
                    mock_t3.assert_not_called()
                    assert result.tier == ResolutionTier.CONFLICT_ONLY_AI

    @pytest.mark.asyncio
    async def test_can_force_specific_tier(self):
        """Test that a specific tier can be forced (skip lower tiers)."""
        from gobby.worktrees.merge.resolver import MergeResolver, ResolutionTier

        resolver = MergeResolver()

        with patch.object(resolver, "_git_merge", new_callable=AsyncMock) as mock_git:
            with patch.object(resolver, "_resolve_full_file", new_callable=AsyncMock) as mock_t3:
                mock_t3.return_value = {"success": True}

                result = await resolver.resolve(
                    worktree_path="/path/to/worktree",
                    source_branch="feature/test",
                    target_branch="main",
                    force_tier=ResolutionTier.FULL_FILE_AI,
                )

                # Should have skipped git and tier 2
                mock_git.assert_not_called()
                assert result.tier == ResolutionTier.FULL_FILE_AI


# =============================================================================
# MergeResult Tests
# =============================================================================


class TestMergeResult:
    """Tests for MergeResult dataclass."""

    def test_merge_result_has_required_fields(self) -> None:
        """Test that MergeResult has all required fields."""
        from gobby.worktrees.merge.resolver import MergeResult, ResolutionTier

        result = MergeResult(
            success=True,
            tier=ResolutionTier.GIT_AUTO,
            conflicts=[],
            resolved_files=[],
            unresolved_conflicts=[],
            needs_human_review=False,
        )

        assert result.success is True
        assert result.tier == ResolutionTier.GIT_AUTO
        assert result.conflicts == []

    def test_merge_result_to_dict(self) -> None:
        """Test MergeResult serialization to dict."""
        from gobby.worktrees.merge.resolver import MergeResult, ResolutionTier

        result = MergeResult(
            success=True,
            tier=ResolutionTier.CONFLICT_ONLY_AI,
            conflicts=[],
            resolved_files=["a.py"],
            unresolved_conflicts=[],
            needs_human_review=False,
        )

        result_dict = result.to_dict()

        assert isinstance(result_dict, dict)
        assert result_dict["success"] is True
        assert "tier" in result_dict


# =============================================================================
# Public API Tests
# =============================================================================


class TestResolveFile:
    """Tests for resolve_file() public method."""

    @pytest.mark.asyncio
    async def test_resolve_file_basic(self):
        """Test resolve_file delegates to appropriate strategy."""
        from gobby.worktrees.merge.resolver import MergeResolver, ResolutionTier

        resolver = MergeResolver()

        with patch.object(resolver, "_resolve_conflicts_only", new_callable=AsyncMock) as mock_t2:
            mock_t2.return_value = {"success": True}

            result = await resolver.resolve_file(
                path="test.py",
                conflict_hunks=["conflict"],
            )

            assert result.success is True
            assert result.tier == ResolutionTier.CONFLICT_ONLY_AI
            assert result.resolved_files == ["test.py"]
            mock_t2.assert_called_once()

    @pytest.mark.asyncio
    async def test_resolve_file_escalates_to_full_file(self):
        """Test resolve_file escalates to full file resolution."""
        from gobby.worktrees.merge.resolver import MergeResolver, ResolutionTier

        resolver = MergeResolver()

        with patch.object(resolver, "_resolve_conflicts_only", new_callable=AsyncMock) as mock_t2:
            with patch.object(resolver, "_resolve_full_file", new_callable=AsyncMock) as mock_t3:
                mock_t2.return_value = {"success": False}
                mock_t3.return_value = {"success": True}

                result = await resolver.resolve_file(
                    path="test.py",
                    conflict_hunks=["conflict"],
                )

                assert result.success is True
                assert result.tier == ResolutionTier.FULL_FILE_AI
                mock_t3.assert_called_once()

    @pytest.mark.asyncio
    async def test_resolve_file_large_conflict_skips_tier2(self):
        """Test that large conflicts skip conflict-only resolution."""
        from gobby.worktrees.merge.resolver import MergeResolver, ResolutionTier

        # Threshold of 5 lines
        resolver = MergeResolver(conflict_size_threshold=5)

        # 10 lines of conflict
        large_conflict = {"ours": "line\n" * 5, "theirs": "line\n" * 5}

        with patch.object(resolver, "_resolve_conflicts_only", new_callable=AsyncMock) as mock_t2:
            with patch.object(resolver, "_resolve_full_file", new_callable=AsyncMock) as mock_t3:
                mock_t3.return_value = {"success": True}

                result = await resolver.resolve_file(
                    path="test.py",
                    conflict_hunks=[large_conflict],
                )

                # Should skip tier 2 because conflict is too large
                mock_t2.assert_not_called()
                mock_t3.assert_called_once()
                assert result.tier == ResolutionTier.FULL_FILE_AI


# =============================================================================
# Edge Case Tests
# =============================================================================


class TestResolverEdgeCases:
    """Edge cases for resolver main flow."""

    @pytest.mark.asyncio
    async def test_resolve_no_git_no_conflicts(self):
        """Test unusual state: no git success but no conflicts returned."""
        from gobby.worktrees.merge.resolver import MergeResolver, ResolutionTier

        resolver = MergeResolver()

        with patch.object(resolver, "_git_merge", new_callable=AsyncMock) as mock_git:
            # Git fails (success=False) but reports no conflicts
            mock_git.return_value = {"success": False, "conflicts": []}

            result = await resolver.resolve(
                worktree_path="/path/to/worktree",
                source_branch="feat",
                target_branch="main",
            )

            assert result.success is True
            # Should default to GIT_AUTO tier as fallback for "no work needed"
            assert result.tier == ResolutionTier.GIT_AUTO
            assert len(result.conflicts) == 0

    @pytest.mark.asyncio
    async def test_parallel_resolution_handles_exceptions(self):
        """Test parallel resolution catches exceptions from individual tasks."""
        from gobby.worktrees.merge.resolver import MergeResolver

        resolver = MergeResolver()

        with patch.object(
            resolver, "_resolve_file_conflict", new_callable=AsyncMock
        ) as mock_resolve:
            # One succeeds, one raises exception
            mock_resolve.side_effect = [
                {"success": True},
                ValueError("Unexpected error"),
            ]

            conflicts = [
                {"file": "a.py"},
                {"file": "b.py"},
            ]

            # Should not raise exception
            resolved, unresolved = await resolver.resolve_conflicts_parallel(
                worktree_path="/path",
                conflicts=conflicts,
            )

            # First file resolved
            assert "a.py" in resolved
            # Second file likely implicitly treated as unresolved or just logged
            # Check implementation: exceptions are caught and logged, not added to unresolved/resolved
            assert len(resolved) == 1
            assert len(unresolved) == 0  # b.py dropped due to exception
