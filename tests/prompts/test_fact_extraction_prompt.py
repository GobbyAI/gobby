"""Tests for fact extraction prompt template."""

from pathlib import Path

import pytest

from gobby.prompts.loader import PromptLoader
from gobby.prompts.sync import sync_bundled_prompts
from gobby.storage.database import LocalDatabase
from tests.fixtures.migrations import run_migrations

pytestmark = pytest.mark.unit

# Bundled prompts directory
PROMPTS_DIR = (
    Path(__file__).parent.parent.parent / "src" / "gobby" / "install" / "shared" / "prompts"
)


class TestFactExtractionPrompt:
    """Tests for memory/fact_extraction prompt template."""

    @pytest.fixture
    def loader(self, tmp_path) -> PromptLoader:
        """Create a DB-backed PromptLoader with bundled prompts synced."""
        db = LocalDatabase(tmp_path / "test.db")
        run_migrations(db)
        sync_bundled_prompts(db)
        return PromptLoader(db=db)

    def test_prompt_file_exists(self) -> None:
        """fact_extraction.md exists in the prompts directory."""
        prompt_path = PROMPTS_DIR / "memory" / "fact_extraction.md"
        assert prompt_path.exists(), f"Expected {prompt_path} to exist"

    def test_prompt_loads_successfully(self, loader: PromptLoader) -> None:
        """PromptLoader can load memory/fact_extraction."""
        template = loader.load("memory/fact_extraction")
        assert template is not None
        assert template.content  # Non-empty content

    def test_prompt_has_content_variable(self, loader: PromptLoader) -> None:
        """Prompt uses {{ content }} Jinja2 variable."""
        template = loader.load("memory/fact_extraction")
        assert "content" in template.variables or "{{ content }}" in template.content

    def test_prompt_renders_with_content(self, loader: PromptLoader) -> None:
        """PromptLoader.render works with content variable."""
        rendered = loader.render(
            "memory/fact_extraction",
            {"content": "The user prefers Python 3.13 and uses pytest for testing."},
        )
        assert "Python 3.13" in rendered
        assert "pytest" in rendered

    def test_prompt_mentions_facts_output_format(self, loader: PromptLoader) -> None:
        """Prompt instructs output as {"facts": [...]} JSON."""
        template = loader.load("memory/fact_extraction")
        assert '"facts"' in template.content

    def test_prompt_instructs_atomic_facts(self, loader: PromptLoader) -> None:
        """Prompt instructs extraction of atomic, self-contained facts."""
        template = loader.load("memory/fact_extraction")
        content_lower = template.content.lower()
        assert "atomic" in content_lower or "self-contained" in content_lower
