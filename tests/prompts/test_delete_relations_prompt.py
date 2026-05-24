"""Tests for delete relations prompt template."""

from pathlib import Path

import pytest

from gobby.prompts.loader import PromptLoader
from gobby.prompts.sync import sync_bundled_prompts
from gobby.storage.hub.protocol import HubDatabase
from tests.fixtures.migrations import run_migrations

pytestmark = pytest.mark.unit

PROMPTS_DIR = (
    Path(__file__).parent.parent.parent / "src" / "gobby" / "install" / "shared" / "prompts"
)


class TestDeleteRelationsPrompt:
    """Tests for memory/delete_relations prompt template."""

    @pytest.fixture
    def loader(self, tmp_path) -> PromptLoader:
        """Create a DB-backed PromptLoader with bundled prompts synced."""
        db = HubDatabase(tmp_path / "test.db")
        run_migrations(db)
        sync_bundled_prompts(db)
        return PromptLoader(db=db)

    def test_prompt_file_exists(self) -> None:
        """delete_relations.md exists in the prompts directory."""
        prompt_path = PROMPTS_DIR / "memory" / "delete_relations.md"
        assert prompt_path.exists(), f"Expected {prompt_path} to exist"

    def test_prompt_loads_successfully(self, loader: PromptLoader) -> None:
        """PromptLoader can load memory/delete_relations."""
        template = loader.load("memory/delete_relations")
        assert template is not None
        assert template.content

    def test_prompt_has_required_variables(self, loader: PromptLoader) -> None:
        """Prompt uses {{ existing_relations }} and {{ new_relations }} variables."""
        template = loader.load("memory/delete_relations")
        assert "{{ existing_relations }}" in template.content
        assert "{{ new_relations }}" in template.content

    def test_prompt_renders_with_variables(self, loader: PromptLoader) -> None:
        """PromptLoader.render works with existing_relations and new_relations."""
        rendered = loader.render(
            "memory/delete_relations",
            {
                "existing_relations": '[{"source": "Josh", "relationship": "uses", "destination": "Python 3.12"}]',
                "new_relations": '[{"source": "Josh", "relationship": "uses", "destination": "Python 3.13"}]',
            },
        )
        assert "Python 3.12" in rendered
        assert "Python 3.13" in rendered

    def test_prompt_specifies_deletion_output(self, loader: PromptLoader) -> None:
        """Prompt instructs output identifying relations to delete."""
        template = loader.load("memory/delete_relations")
        assert "delete" in template.content.lower()
