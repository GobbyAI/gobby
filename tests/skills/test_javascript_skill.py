"""Contract tests for the bundled JavaScript language skill."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.search.models import SearchConfig
from gobby.skills.loader import SkillLoader
from gobby.skills.manager import SkillManager
from gobby.skills.search import SkillSearch
from gobby.skills.sync import sync_bundled_skills
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/javascript"


def test_javascript_skill_parses_with_references() -> None:
    """Verify the bundled JavaScript skill has expected metadata and reference files."""
    parsed = SkillLoader().load_skill(SKILL_DIR, validate=True)

    assert parsed.name == "javascript"
    assert parsed.version == "1.1.0"
    assert parsed.get_category() == "development"
    assert parsed.triggers is not None
    assert {"javascript", "js", "jsx", "package.json"}.issubset(parsed.triggers)
    assert 'get_skill_file(name="javascript", path="references/configuration.md")' in (
        parsed.content
    )

    assert parsed.loaded_files is not None
    reference_paths = {file.path for file in parsed.loaded_files if file.file_type == "reference"}
    assert reference_paths == {
        "references/async.md",
        "references/configuration.md",
        "references/error-handling.md",
        "references/performance.md",
        "references/testing.md",
        "references/types.md",
    }


def test_synced_javascript_skill_is_searchable(temp_db: HubDatabase) -> None:
    """Verify bundled sync makes JavaScript discoverable through skill search."""
    result = sync_bundled_skills(temp_db)
    assert result["success"] is True

    manager = SkillManager(temp_db)
    skill = manager.get_by_name("javascript")
    assert skill is not None
    assert skill.source == "installed"
    assert skill.source_type == "filesystem"

    search = SkillSearch(db=temp_db, config=SearchConfig(mode="keyword"))
    search.index_skills([skill])

    results = search.search("javascript runtime boundaries", top_k=3)
    assert results
    assert results[0].skill_name == "javascript"
