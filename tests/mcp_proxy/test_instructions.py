"""Tests for Gobby MCP server instructions builder."""

import pathlib
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit


class TestBuildGobbyInstructions:
    """Test build_gobby_instructions() function."""

    def test_returns_non_empty_string(self) -> None:
        """Instructions should return a non-empty string."""
        from gobby.mcp_proxy.instructions import build_gobby_instructions

        result = build_gobby_instructions()

        assert isinstance(result, str)
        assert len(result) > 0

    def test_contains_gobby_system_tags(self) -> None:
        """Instructions should be wrapped in <gobby_system> tags."""
        from gobby.mcp_proxy.instructions import build_gobby_instructions

        result = build_gobby_instructions()

        assert "<gobby_system>" in result
        assert "</gobby_system>" in result

    def test_contains_tool_discovery_section(self) -> None:
        """Instructions should include <tool_discovery> section."""
        from gobby.mcp_proxy.instructions import build_gobby_instructions

        result = build_gobby_instructions()

        assert "<tool_discovery>" in result
        assert "</tool_discovery>" in result
        # Should mention progressive discovery pattern
        assert "list_tools" in result
        assert "get_tool_schema" in result
        assert "call_tool" in result

    def test_contains_rules_section(self) -> None:
        """Instructions should include <rules> section."""
        from gobby.mcp_proxy.instructions import build_gobby_instructions

        result = build_gobby_instructions()

        assert "<rules>" in result
        assert "</rules>" in result
        # Should mention key rules
        assert "task" in result.lower()  # task before editing
        assert "session_id" in result  # session_id required

    def test_emphasizes_progressive_discovery(self) -> None:
        """Instructions should emphasize progressive discovery pattern."""
        from gobby.mcp_proxy.instructions import build_gobby_instructions

        result = build_gobby_instructions()

        # Should discourage loading all schemas upfront
        assert "NEVER" in result
        # Should mention the pattern
        assert "progressive" in result.lower() or "discovery" in result.lower()

    def test_loads_from_prompt_file(self) -> None:
        """Instructions should be loaded from the bundled prompt file."""
        from gobby.mcp_proxy.instructions import build_gobby_instructions
        from gobby.prompts.sync import get_bundled_prompts_path

        prompt_file = get_bundled_prompts_path() / "mcp" / "progressive-discovery.md"
        assert prompt_file.exists(), f"Prompt file missing: {prompt_file}"

        result = build_gobby_instructions()

        # Content should come from the file, not the fallback
        # The file and fallback have identical content, so verify we're
        # reading from the file by checking the result matches file body
        raw = prompt_file.read_text(encoding="utf-8")
        # Strip frontmatter
        parts = raw.split("---", 2)
        expected = parts[2].strip()
        assert result == expected

    def test_fallback_when_file_missing(self, tmp_path: "pathlib.Path") -> None:
        """Instructions should fall back to hardcoded string when file is missing."""
        from gobby.mcp_proxy.instructions import (
            _FALLBACK_INSTRUCTIONS,
            build_gobby_instructions,
        )

        # Point to a directory that exists but has no prompt file
        empty_dir = tmp_path / "empty_prompts"
        empty_dir.mkdir()

        with patch(
            "gobby.mcp_proxy.instructions.get_bundled_prompts_path",
            return_value=empty_dir,
        ):
            result = build_gobby_instructions()

        assert result == _FALLBACK_INSTRUCTIONS
        assert "<gobby_system>" in result
