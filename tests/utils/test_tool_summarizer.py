"""Tests for the tool description summarizer module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.utils.tool_summarizer import (
    MAX_DESCRIPTION_LENGTH,
    summarize_tools,
)

pytestmark = pytest.mark.unit


class TestSummarizeTools:
    """Tests for summarize_tools function."""

    @pytest.mark.asyncio
    async def test_short_descriptions_unchanged(self):
        """Test that short descriptions are not modified."""
        tool1 = MagicMock()
        tool1.name = "tool1"
        tool1.description = "Short description"
        tool1.inputSchema = {"type": "object"}

        tool2 = MagicMock()
        tool2.name = "tool2"
        tool2.description = "Another short one"
        tool2.inputSchema = {"type": "string"}

        result = await summarize_tools([tool1, tool2])

        assert len(result) == 2
        assert result[0]["name"] == "tool1"
        assert result[0]["description"] == "Short description"
        assert result[1]["name"] == "tool2"
        assert result[1]["description"] == "Another short one"

    @pytest.mark.asyncio
    async def test_empty_description(self):
        """Test handling of empty description."""
        tool = MagicMock()
        tool.name = "no_desc"
        tool.description = ""
        tool.inputSchema = {}

        result = await summarize_tools([tool])

        assert len(result) == 1
        assert result[0]["description"] == ""

    @pytest.mark.asyncio
    async def test_none_description(self):
        """Test handling of None description."""
        tool = MagicMock()
        tool.name = "null_desc"
        tool.description = None
        tool.inputSchema = {}

        result = await summarize_tools([tool])

        assert len(result) == 1
        assert result[0]["description"] == ""

    @pytest.mark.asyncio
    async def test_missing_input_schema(self):
        """Test handling of tool without inputSchema attribute."""
        tool = MagicMock(spec=["name", "description"])
        tool.name = "no_schema"
        tool.description = "Has desc"

        result = await summarize_tools([tool])

        assert len(result) == 1
        assert result[0]["args"] == {}

    @pytest.mark.asyncio
    async def test_preserves_input_schema(self):
        """Test that inputSchema is preserved in output."""
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        }

        tool = MagicMock()
        tool.name = "with_schema"
        tool.description = "desc"
        tool.inputSchema = schema

        result = await summarize_tools([tool])

        assert result[0]["args"] == schema

    @pytest.mark.asyncio
    async def test_long_description_triggers_summarization(self):
        """Test that long descriptions trigger summarization attempt."""
        long_desc = "X" * 250  # Over MAX_DESCRIPTION_LENGTH

        tool = MagicMock()
        tool.name = "long_tool"
        tool.description = long_desc
        tool.inputSchema = {"type": "object"}

        # Mock the summarization function to return a fallback
        with patch(
            "gobby.utils.tool_summarizer._summarize_description_with_llm",
            new_callable=AsyncMock,
            return_value="Shortened description",
        ) as mock_summarize:
            result = await summarize_tools([tool])

            assert len(result) == 1
            assert result[0]["name"] == "long_tool"
            assert result[0]["description"] == "Shortened description"
            mock_summarize.assert_called_once_with(long_desc)


class TestMaxDescriptionLength:
    """Tests for MAX_DESCRIPTION_LENGTH constant."""

    def test_max_length_value(self) -> None:
        """Test that MAX_DESCRIPTION_LENGTH is set to 200."""
        assert MAX_DESCRIPTION_LENGTH == 200

    @pytest.mark.asyncio
    async def test_exact_boundary_length(self):
        """Test description exactly at the boundary."""
        exact_desc = "Z" * 200  # Exactly MAX_DESCRIPTION_LENGTH

        tool = MagicMock()
        tool.name = "exact"
        tool.description = exact_desc
        tool.inputSchema = {}

        with patch(
            "gobby.utils.tool_summarizer._summarize_description_with_llm",
            new_callable=AsyncMock,
        ) as mock_summarize:
            result = await summarize_tools([tool])

            # Should NOT be summarized (len == 200, not > 200)
            mock_summarize.assert_not_called()
            assert result[0]["description"] == exact_desc

    @pytest.mark.asyncio
    async def test_one_over_boundary(self):
        """Test description one character over the boundary."""
        over_desc = "Z" * 201  # One over MAX_DESCRIPTION_LENGTH

        tool = MagicMock()
        tool.name = "over"
        tool.description = over_desc
        tool.inputSchema = {}

        with patch(
            "gobby.utils.tool_summarizer._summarize_description_with_llm",
            new_callable=AsyncMock,
            return_value="Short",
        ) as mock_summarize:
            await summarize_tools([tool])

            # Should be summarized
            mock_summarize.assert_called_once()


class TestSummarizeDescriptionWithClaude:
    """Tests for _summarize_description_with_llm function."""

    @pytest.mark.asyncio
    async def test_summarize_description_success(self):
        """Test successful summarization via configured provider."""
        from gobby.utils.tool_summarizer import _summarize_description_with_llm

        mock_config = MagicMock()
        mock_config.prompt_path = "features/tool_summary"
        mock_config.system_prompt_path = "features/tool_summary_system"
        mock_config.model = "claude-3-haiku-20240307"

        mock_llm_service = MagicMock()
        mock_llm_service.call_feature = AsyncMock(return_value="Summarized text")

        mock_loader = MagicMock()
        mock_loader.render.return_value = "Long description " * 10

        with (
            patch("gobby.utils.tool_summarizer._get_config", return_value=mock_config),
            patch("gobby.utils.tool_summarizer._loader", mock_loader),
            patch("gobby.utils.tool_summarizer._llm_service", mock_llm_service),
        ):
            result = await _summarize_description_with_llm("Long description " * 10)
            assert result == "Summarized text"
            mock_llm_service.call_feature.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_summarize_description_failure_fallback(self):
        """Test fallback when summarization fails."""
        from gobby.utils.tool_summarizer import _summarize_description_with_llm

        with (
            patch("gobby.utils.tool_summarizer._get_config"),
            patch("gobby.utils.tool_summarizer._llm_service", None),
        ):
            long_desc = "A" * 250
            result = await _summarize_description_with_llm(long_desc)

            # Should truncate
            assert len(result) == 200
            assert result.endswith("...")
            assert result.startswith("AAAA")


class TestGenerateServerDescription:
    """Tests for generate_server_description function."""

    @pytest.mark.asyncio
    async def test_generate_server_description_success(self):
        """Test successful server description generation."""
        from gobby.utils.tool_summarizer import generate_server_description

        tool_summaries = [{"name": "tool1", "description": "desc1"}]

        mock_config = MagicMock()
        mock_config.server_description_prompt_path = "features/server_description"
        mock_config.server_description_system_prompt_path = "features/server_description_system"
        mock_config.model = "model"

        mock_llm_service = MagicMock()
        mock_llm_service.call_feature = AsyncMock(return_value="Server does things.")

        mock_loader = MagicMock()
        mock_loader.render.return_value = "Describe server1"

        with (
            patch("gobby.utils.tool_summarizer._get_config", return_value=mock_config),
            patch("gobby.utils.tool_summarizer._loader", mock_loader),
            patch("gobby.utils.tool_summarizer._llm_service", mock_llm_service),
        ):
            result = await generate_server_description("server1", tool_summaries)
            assert result == "Server does things."

    @pytest.mark.asyncio
    async def test_generate_server_description_failure_fallback(self):
        """Test fallback when generation fails."""
        from gobby.utils.tool_summarizer import generate_server_description

        tool_summaries = [
            {"name": "tool1", "description": "desc1"},
            {"name": "tool2", "description": "desc2"},
            {"name": "tool3", "description": "desc3"},
            {"name": "tool4", "description": "desc4"},
        ]

        with patch("gobby.utils.tool_summarizer._llm_service", None):
            result = await generate_server_description("server1", tool_summaries)
            assert "Provides tool1, tool2, tool3 and more" in result

    @pytest.mark.asyncio
    async def test_generate_server_description_fallback_no_tools(self):
        """Test fallback with no tools."""
        from gobby.utils.tool_summarizer import generate_server_description

        with patch("gobby.utils.tool_summarizer._llm_service", None):
            result = await generate_server_description("server1", [])
            assert result == "MCP server: server1"
