"""Tests for the ToolFallbackResolver module."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.services.fallback import FallbackSuggestion, ToolFallbackResolver

pytestmark = pytest.mark.unit


class TestFallbackSuggestion:
    """Tests for FallbackSuggestion dataclass."""

    def test_to_dict(self) -> None:
        """Test conversion to dictionary."""
        suggestion = FallbackSuggestion(
            server_name="test-server",
            tool_name="test_tool",
            description="A test tool",
            similarity=0.85,
            success_rate=0.92,
            score=0.87,
        )

        result = suggestion.to_dict()

        assert result["server_name"] == "test-server"
        assert result["tool_name"] == "test_tool"
        assert result["description"] == "A test tool"
        assert result["similarity"] == 0.85
        assert result["success_rate"] == 0.92
        assert result["score"] == 0.87

    def test_to_dict_with_none_success_rate(self) -> None:
        """Test conversion with None success_rate."""
        suggestion = FallbackSuggestion(
            server_name="test-server",
            tool_name="test_tool",
            description=None,
            similarity=0.75,
            success_rate=None,
            score=0.75,
        )

        result = suggestion.to_dict()

        assert result["success_rate"] is None
        assert result["description"] is None

    def test_to_dict_preserves_zero_success_rate(self) -> None:
        suggestion = FallbackSuggestion(
            server_name="test-server",
            tool_name="failing_tool",
            description="Always fails",
            similarity=0.75,
            success_rate=0.0,
            score=0.45,
        )

        result = suggestion.to_dict()

        assert result["success_rate"] == 0.0


class TestToolFallbackResolver:
    """Tests for ToolFallbackResolver."""

    @pytest.fixture
    def mock_semantic_search(self):
        """Create a mock semantic search."""
        mock = MagicMock()
        mock.search_tools = AsyncMock(return_value=[])
        mock.db = MagicMock()
        mock.db.fetchone = MagicMock(return_value=None)
        return mock

    @pytest.fixture
    def mock_metrics_manager(self):
        """Create a mock metrics manager."""
        mock = MagicMock()
        mock.get_tool_success_rate = MagicMock(return_value=0.9)
        return mock

    @pytest.fixture
    def fallback_resolver(self, mock_semantic_search, mock_metrics_manager):
        """Create a fallback resolver with mocks."""
        return ToolFallbackResolver(
            semantic_search=mock_semantic_search,
            metrics_manager=mock_metrics_manager,
        )

    async def test_find_alternatives_no_project_id(self, fallback_resolver):
        """Test that find_alternatives returns empty without project_id."""
        result = await fallback_resolver.find_alternatives(
            failed_tool_name="test_tool",
            project_id=None,
        )

        assert result == []

    async def test_find_alternatives_no_matches(self, fallback_resolver, mock_semantic_search):
        """Test find_alternatives when semantic search returns no results."""
        mock_semantic_search.search_tools = AsyncMock(return_value=[])

        result = await fallback_resolver.find_alternatives(
            failed_tool_name="test_tool",
            project_id="test-project",
        )

        assert result == []

    async def test_find_alternatives_with_matches(self, fallback_resolver, mock_semantic_search):
        """Test find_alternatives with matching tools."""
        # Create mock search results
        mock_result = MagicMock()
        mock_result.server_name = "alt-server"
        mock_result.tool_name = "alt_tool"
        mock_result.description = "An alternative tool"
        mock_result.similarity = 0.8

        mock_semantic_search.search_tools = AsyncMock(return_value=[mock_result])

        result = await fallback_resolver.find_alternatives(
            failed_tool_name="test_tool",
            project_id="test-project",
        )

        assert len(result) == 1
        assert result[0].server_name == "alt-server"
        assert result[0].tool_name == "alt_tool"
        assert result[0].similarity == 0.8

    async def test_find_alternatives_excludes_failed_tool(
        self, fallback_resolver, mock_semantic_search
    ):
        """Test that the failed tool is excluded from results."""
        mock_result1 = MagicMock()
        mock_result1.server_name = "test-server"
        mock_result1.tool_name = "test_tool"  # Same as failed
        mock_result1.description = "The failed tool"
        mock_result1.similarity = 0.95

        mock_result2 = MagicMock()
        mock_result2.server_name = "alt-server"
        mock_result2.tool_name = "alt_tool"
        mock_result2.description = "An alternative"
        mock_result2.similarity = 0.7

        mock_semantic_search.search_tools = AsyncMock(return_value=[mock_result1, mock_result2])

        result = await fallback_resolver.find_alternatives(
            failed_tool_name="test_tool",
            server_name="test-server",
            project_id="test-project",
            exclude_failed=True,
        )

        assert len(result) == 1
        assert result[0].tool_name == "alt_tool"

    async def test_find_alternatives_includes_failed_when_not_excluded(
        self, fallback_resolver, mock_semantic_search
    ):
        """Test that failed tool is included when exclude_failed=False."""
        mock_result = MagicMock()
        mock_result.server_name = "test-server"
        mock_result.tool_name = "test_tool"
        mock_result.description = "The tool"
        mock_result.similarity = 0.9

        mock_semantic_search.search_tools = AsyncMock(return_value=[mock_result])

        result = await fallback_resolver.find_alternatives(
            failed_tool_name="test_tool",
            server_name="test-server",
            project_id="test-project",
            exclude_failed=False,
        )

        assert len(result) == 1
        assert result[0].tool_name == "test_tool"

    def test_compute_score_with_success_rate(self, fallback_resolver) -> None:
        """Test score computation with success rate."""
        score = fallback_resolver._compute_score(
            similarity=0.8,
            success_rate=0.9,
        )

        # Default weights: similarity=0.7, success=0.3
        expected = 0.8 * 0.7 + 0.9 * 0.3
        assert abs(score - expected) < 0.001

    def test_compute_score_without_success_rate(self, fallback_resolver) -> None:
        """Test score computation with None success rate uses default."""
        score = fallback_resolver._compute_score(
            similarity=0.8,
            success_rate=None,
        )

        # Uses default success rate of 0.5
        expected = 0.8 * 0.7 + 0.5 * 0.3
        assert abs(score - expected) < 0.001

    def test_build_search_query_basic(self, fallback_resolver) -> None:
        """Test building search query with just tool name."""
        query = fallback_resolver._build_search_query(
            tool_name="test_tool",
            description=None,
            error_context=None,
        )

        assert "test_tool" in query

    def test_build_search_query_with_description(self, fallback_resolver) -> None:
        """Test building search query with description."""
        query = fallback_resolver._build_search_query(
            tool_name="test_tool",
            description="A tool for testing",
            error_context=None,
        )

        assert "test_tool" in query
        assert "A tool for testing" in query

    def test_build_search_query_with_error_context(self, fallback_resolver) -> None:
        """Test building search query with error context."""
        query = fallback_resolver._build_search_query(
            tool_name="test_tool",
            description=None,
            error_context="Connection refused",
        )

        assert "test_tool" in query
        assert "Connection refused" in query

    async def test_find_alternatives_for_error(self, fallback_resolver, mock_semantic_search):
        """Test the convenience method for error handling."""
        mock_result = MagicMock()
        mock_result.server_name = "alt-server"
        mock_result.tool_name = "alt_tool"
        mock_result.description = "Alternative"
        mock_result.similarity = 0.75

        mock_semantic_search.search_tools = AsyncMock(return_value=[mock_result])

        result = await fallback_resolver.find_alternatives_for_error(
            server_name="test-server",
            tool_name="test_tool",
            error_message="Tool execution failed",
            project_id="test-project",
            top_k=3,
        )

        assert len(result) == 1
        assert result[0]["tool_name"] == "alt_tool"
        assert "score" in result[0]

    async def test_find_alternatives_handles_search_error(
        self, fallback_resolver, mock_semantic_search
    ):
        """Test graceful handling of semantic search errors."""
        mock_semantic_search.search_tools = AsyncMock(side_effect=Exception("Search failed"))

        result = await fallback_resolver.find_alternatives(
            failed_tool_name="test_tool",
            project_id="test-project",
        )

        assert result == []

    def test_get_success_rate_returns_none_without_metrics(self, mock_semantic_search) -> None:
        """Test that success rate returns None without metrics manager."""
        resolver = ToolFallbackResolver(
            semantic_search=mock_semantic_search,
            metrics_manager=None,
        )

        rate = resolver._get_success_rate("server", "tool", "project")

        assert rate is None

    def test_get_success_rate_handles_error(self, fallback_resolver, mock_metrics_manager) -> None:
        """Test graceful handling of metrics lookup errors."""
        mock_metrics_manager.get_tool_success_rate = MagicMock(side_effect=Exception("DB error"))

        rate = fallback_resolver._get_success_rate("server", "tool", "project")

        assert rate is None
