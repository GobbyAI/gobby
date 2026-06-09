"""Contract tests for the bundled Dart language skill."""

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
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/dart"


def test_dart_skill_parses_with_references() -> None:
    """Verify the bundled Dart skill has expected metadata and reference files."""
    parsed = SkillLoader().load_skill(SKILL_DIR, validate=False)

    assert parsed.name == "dart"
    assert parsed.version == "1.0.0"
    assert parsed.get_category() == "development"
    assert parsed.triggers is not None
    assert {"dart", "flutter", "pubspec.yaml", "analysis_options.yaml"}.issubset(parsed.triggers)
    assert 'get_skill_file(name="dart", path="references/configuration.md")' in (parsed.content)
    assert 'get_skill_file(name="dart", path="references/flutter-boundaries.md")' in (
        parsed.content
    )

    assert parsed.loaded_files is not None
    reference_paths = {file.path for file in parsed.loaded_files if file.file_type == "reference"}
    assert reference_paths == {
        "references/async-and-errors.md",
        "references/configuration.md",
        "references/flutter-boundaries.md",
        "references/performance.md",
        "references/serialization.md",
        "references/testing.md",
        "references/types.md",
    }


def test_synced_dart_skill_is_searchable(temp_db: HubDatabase) -> None:
    """Verify bundled sync makes Dart discoverable through skill search."""
    result = sync_bundled_skills(temp_db)
    assert result["success"] is True

    manager = SkillManager(temp_db)
    skill = manager.get_by_name("dart")
    assert skill is not None
    assert skill.source == "installed"
    assert skill.source_type == "filesystem"

    search = SkillSearch(db=temp_db, config=SearchConfig(mode="keyword"))
    search.index_skills([skill])

    results = search.search("dart flutter pubspec analyzer widget null safety", top_k=3)
    assert results
    assert results[0].skill_name == "dart"
