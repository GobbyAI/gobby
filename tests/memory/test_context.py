"""Tests for memory context building."""

import pytest

from gobby.memory.context import (
    _strip_leading_bullet,
    build_memory_context,
)
from gobby.storage.memories import Memory

pytestmark = pytest.mark.unit


class TestStripLeadingBullet:
    """Tests for _strip_leading_bullet function."""

    def test_dash_bullet(self) -> None:
        """Test stripping dash bullet."""
        assert _strip_leading_bullet("- item") == "item"

    def test_asterisk_bullet(self) -> None:
        """Test stripping asterisk bullet."""
        assert _strip_leading_bullet("* item") == "item"

    def test_bullet_point_character(self) -> None:
        """Test stripping bullet point character."""
        assert _strip_leading_bullet("• item") == "item"

    def test_no_bullet(self) -> None:
        """Test content without bullet."""
        assert _strip_leading_bullet("item") == "item"

    def test_leading_whitespace(self) -> None:
        """Test content with leading whitespace."""
        assert _strip_leading_bullet("  - item") == "item"
        assert _strip_leading_bullet("  item") == "item"

    def test_empty_content(self) -> None:
        """Test empty content."""
        assert _strip_leading_bullet("") == ""
        assert _strip_leading_bullet("   ") == ""

    def test_only_bullet(self) -> None:
        """Test content that is only a bullet marker."""
        # Single dash without space is stripped (matches bullet pattern)
        assert _strip_leading_bullet("-") == ""
        assert _strip_leading_bullet("- ") == ""
        assert _strip_leading_bullet("*") == ""
        assert _strip_leading_bullet("•") == ""

    def test_preserves_internal_dashes(self) -> None:
        """Test that internal dashes are preserved."""
        assert _strip_leading_bullet("- use foo-bar") == "use foo-bar"
        assert _strip_leading_bullet("use foo-bar") == "use foo-bar"


class TestBuildMemoryContext:
    """Tests for build_memory_context function."""

    def test_empty_memories(self) -> None:
        """Test with empty memory list."""
        assert build_memory_context([]) == ""

    def test_single_preference(self) -> None:
        """Test with single preference memory."""
        mem = Memory(
            id="m1",
            content="- Use TypeScript",
            memory_type="preference",
            created_at="2024-01-01",
            updated_at="2024-01-01",
        )
        result = build_memory_context([mem])
        assert "## Preferences" in result
        assert "- Use TypeScript" in result
        # Should not have double bullets
        assert "- - " not in result

    def test_handles_various_bullet_formats(self) -> None:
        """Test that various bullet formats are normalized."""
        memories = [
            Memory(
                id="m1",
                content="- dash item",
                memory_type="preference",
                created_at="2024-01-01",
                updated_at="2024-01-01",
            ),
            Memory(
                id="m2",
                content="* asterisk item",
                memory_type="preference",
                created_at="2024-01-01",
                updated_at="2024-01-01",
            ),
            Memory(
                id="m3",
                content="• bullet item",
                memory_type="preference",
                created_at="2024-01-01",
                updated_at="2024-01-01",
            ),
        ]
        result = build_memory_context(memories)
        # All should be normalized to "- "
        assert "- dash item" in result
        assert "- asterisk item" in result
        assert "- bullet item" in result
        # No double bullets
        assert "- - " not in result
        assert "- * " not in result
        assert "- • " not in result

    def test_all_memory_types(self) -> None:
        """Test with all 4 memory types present."""
        memories = [
            Memory(
                id="m1",
                content="This is the project context",
                memory_type="context",
                created_at="2024-01-01",
                updated_at="2024-01-01",
            ),
            Memory(
                id="m2",
                content="- Use Python 3.11+",
                memory_type="preference",
                created_at="2024-01-01",
                updated_at="2024-01-01",
            ),
            Memory(
                id="m3",
                content="- Follow PEP 8 style",
                memory_type="pattern",
                created_at="2024-01-01",
                updated_at="2024-01-01",
            ),
            Memory(
                id="m4",
                content="- Database uses PostgreSQL",
                memory_type="fact",
                created_at="2024-01-01",
                updated_at="2024-01-01",
            ),
        ]
        result = build_memory_context(memories)

        # Check all sections are present
        assert "<project-memory>" in result
        assert "</project-memory>" in result
        assert "## Project Context" in result
        assert "## Preferences" in result
        assert "## Patterns" in result
        assert "## Facts" in result

        # Check content is included
        assert "This is the project context" in result
        assert "Use Python 3.11+" in result
        assert "Follow PEP 8 style" in result
        assert "Database uses PostgreSQL" in result

    def test_context_type_no_bullet_stripping(self) -> None:
        """Test that context type content is not bullet-stripped."""
        mem = Memory(
            id="m1",
            content="- This is context with dash",
            memory_type="context",
            created_at="2024-01-01",
            updated_at="2024-01-01",
        )
        result = build_memory_context([mem])
        # Context type preserves original formatting
        assert "- This is context with dash" in result

    def test_mixed_types_ordering(self) -> None:
        """Test that sections appear in correct order."""
        memories = [
            Memory(
                id="m1",
                content="fact content",
                memory_type="fact",
                created_at="2024-01-01",
                updated_at="2024-01-01",
            ),
            Memory(
                id="m2",
                content="context content",
                memory_type="context",
                created_at="2024-01-01",
                updated_at="2024-01-01",
            ),
        ]
        result = build_memory_context(memories)

        # Context should appear before Facts
        context_pos = result.find("## Project Context")
        facts_pos = result.find("## Facts")
        assert context_pos < facts_pos

    def test_skips_empty_content_after_stripping(self) -> None:
        """Test that empty content after bullet stripping is skipped."""
        memories = [
            Memory(
                id="m1",
                content="- ",  # Only bullet, no content
                memory_type="preference",
                created_at="2024-01-01",
                updated_at="2024-01-01",
            ),
            Memory(
                id="m2",
                content="- Valid content",
                memory_type="preference",
                created_at="2024-01-01",
                updated_at="2024-01-01",
            ),
        ]
        result = build_memory_context(memories)
        # Should only have one preference item (the valid one)
        assert result.count("- Valid content") == 1
        # Should not have empty bullet lines
        lines = result.split("\n")
        bullet_lines = [line for line in lines if line.strip() == "-"]
        assert len(bullet_lines) == 0
