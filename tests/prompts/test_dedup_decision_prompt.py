"""Tests for dedup decision prompt template."""

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


class TestDedupDecisionPrompt:
    """Tests for memory/dedup_decision prompt template."""

    @pytest.fixture
    def loader(self, temp_db: HubDatabase) -> PromptLoader:
        """Create a DB-backed PromptLoader with bundled prompts synced."""
        db = temp_db
        sync_bundled_prompts(db)
        return PromptLoader(db=db)

    def test_prompt_file_exists(self) -> None:
        """dedup_decision.md exists in the prompts directory."""
        prompt_path = PROMPTS_DIR / "memory" / "dedup_decision.md"
        assert prompt_path.exists(), f"Expected {prompt_path} to exist"

    def test_prompt_loads_successfully(self, loader: PromptLoader) -> None:
        """PromptLoader can load memory/dedup_decision."""
        template = loader.load("memory/dedup_decision")
        assert template is not None
        assert template.content

    def test_prompt_has_required_variables(self, loader: PromptLoader) -> None:
        """Prompt uses new_facts and existing_memories Jinja2 variables."""
        template = loader.load("memory/dedup_decision")
        assert "{{ new_facts }}" in template.content or "new_facts" in template.variables
        assert (
            "{{ existing_memories }}" in template.content
            or "existing_memories" in template.variables
        )

    def test_prompt_renders_with_variables(self, loader: PromptLoader) -> None:
        """PromptLoader.render works with required variables."""
        rendered = loader.render(
            "memory/dedup_decision",
            {
                "new_facts": '["The project uses Python 3.13"]',
                "existing_memories": '[{"id": "mem-1", "text": "The project uses Python 3.12"}]',
            },
        )
        assert "Python 3.13" in rendered
        assert "mem-1" in rendered

    def test_prompt_mentions_action_types(self, loader: PromptLoader) -> None:
        """Prompt instructs ADD/UPDATE/DELETE/NOOP decisions."""
        template = loader.load("memory/dedup_decision")
        assert "ADD" in template.content
        assert "UPDATE" in template.content
        assert "DELETE" in template.content
        assert "NOOP" in template.content

    def test_prompt_mentions_memory_output_format(self, loader: PromptLoader) -> None:
        """Prompt outputs {"memory": [...]} JSON format."""
        template = loader.load("memory/dedup_decision")
        assert '"memory"' in template.content
