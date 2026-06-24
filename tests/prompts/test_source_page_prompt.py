"""Tests for the session knowledge-synthesis (wiki) source_page prompt template."""

from pathlib import Path

import pytest

from gobby.prompts.loader import PromptLoader
from gobby.prompts.sync import sync_bundled_prompts
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

# Bundled prompts directory
PROMPTS_DIR = (
    Path(__file__).parent.parent.parent / "src" / "gobby" / "install" / "shared" / "prompts"
)


class TestSourcePagePrompt:
    """Tests for the wiki/source_page prompt template."""

    @pytest.fixture
    def loader(self, temp_db: HubDatabase) -> PromptLoader:
        """Create a DB-backed PromptLoader with bundled prompts synced."""
        db = temp_db
        sync_bundled_prompts(db)
        return PromptLoader(db=db)

    def test_prompt_file_exists(self) -> None:
        """wiki/source_page.md exists in the prompts directory."""
        prompt_path = PROMPTS_DIR / "wiki" / "source_page.md"
        assert prompt_path.exists(), f"Expected {prompt_path} to exist"

    def test_prompt_loads_successfully(self, loader: PromptLoader) -> None:
        """PromptLoader can load wiki/source_page."""
        template = loader.load("wiki/source_page")
        assert template is not None
        assert template.content

    def test_prompt_has_required_variables(self, loader: PromptLoader) -> None:
        """Prompt is fed the per-turn digest_markdown + meta (NOT a raw transcript)."""
        template = loader.load("wiki/source_page")
        assert (
            "{{ digest_markdown }}" in template.content or "digest_markdown" in template.variables
        )
        assert "{{ meta }}" in template.content or "meta" in template.variables

    def test_prompt_renders_with_variables(self, loader: PromptLoader) -> None:
        """PromptLoader.render injects the digest + meta context."""
        rendered = loader.render(
            "wiki/source_page",
            {
                "digest_markdown": "Turn 1: chose pg_search BM25 for symbol search.",
                "meta": "project: gobby-cli\nmodel: claude-opus-4-8",
            },
        )
        assert "pg_search BM25" in rendered
        assert "claude-opus-4-8" in rendered

    def test_strict_render_uses_default_digest_and_meta(self, loader: PromptLoader) -> None:
        """strict=True renders with default digest_markdown / meta values."""
        rendered = loader.render("wiki/source_page", {}, strict=True)
        assert "{{ digest_markdown }}" not in rendered
        assert "{{ meta }}" not in rendered
        assert "Metadata:" in rendered
        assert "Digest:" in rendered

    def test_prompt_declares_five_synthesis_sections(self, loader: PromptLoader) -> None:
        """Prompt instructs the five llm-wiki knowledge sections."""
        template = loader.load("wiki/source_page")
        for section in (
            "## Summary",
            "## Key Claims",
            "## Key Quotes",
            "## Connections",
            "## Contradictions",
        ):
            assert section in template.content, f"missing {section}"

    def test_prompt_is_body_only_with_wikilinks_and_tags(self, loader: PromptLoader) -> None:
        """Prompt emits BODY ONLY (caller adds frontmatter), uses [[wikilinks]] + suggested-tags."""
        template = loader.load("wiki/source_page")
        assert "suggested-tags" in template.content
        assert "[[wikilinks]]" in template.content
        # Body only: the model must NOT emit frontmatter; the caller wraps it.
        assert "no frontmatter" in template.content.lower()

    def test_prompt_contradictions_use_only_digest_and_meta(self, loader: PromptLoader) -> None:
        """Contradiction guidance is limited to the rendered prompt inputs."""
        template = loader.load("wiki/source_page")
        content = template.content.lower()
        assert "known wiki content" not in content
        assert "outside context" in content
        assert "digest or metadata contains conflicting claims" in content
