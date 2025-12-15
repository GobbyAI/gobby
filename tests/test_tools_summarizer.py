"""Tests for the tool description summarizer module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.tools.summarizer import (
    MAX_DESCRIPTION_LENGTH,
    summarize_tools,
)


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
            "gobby.tools.summarizer._summarize_description_with_claude",
            new_callable=AsyncMock,
            return_value="Shortened description",
        ) as mock_summarize:
            result = await summarize_tools([tool])

            assert len(result) == 1
            assert result[0]["name"] == "long_tool"
            mock_summarize.assert_called_once_with(long_desc)


class TestMaxDescriptionLength:
    """Tests for MAX_DESCRIPTION_LENGTH constant."""

    def test_max_length_value(self):
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
            "gobby.tools.summarizer._summarize_description_with_claude",
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
            "gobby.tools.summarizer._summarize_description_with_claude",
            new_callable=AsyncMock,
            return_value="Short",
        ) as mock_summarize:
            result = await summarize_tools([tool])

            # Should be summarized
            mock_summarize.assert_called_once()
            assert result[0]["description"] == "Short"
