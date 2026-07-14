"""Contract tests for the bundled Swift language skill."""

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
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/swift"


def test_swift_skill_parses_with_references() -> None:
    """Verify the bundled Swift skill has expected metadata and reference files."""
    parsed = SkillLoader().load_skill(SKILL_DIR, validate=False)

    assert parsed.name == "swift"
    assert parsed.version == "1.1.0"
    assert parsed.get_category() == "development"
    assert parsed.triggers is not None
    assert {
        "swift",
        "swiftpm",
        "xcode",
        "swiftui",
        "actors",
        "sendable",
        "swift-testing",
        "swiftlint",
    }.issubset(parsed.triggers)
    assert 'get_skill_file(name="swift", path="references/configuration.md")' in parsed.content
    assert (
        'get_skill_file(name="swift", path="references/concurrency-and-error-handling.md")'
    ) in parsed.content

    assert parsed.loaded_files is not None
    reference_paths = {file.path for file in parsed.loaded_files if file.file_type == "reference"}
    assert reference_paths == {
        "references/configuration.md",
        "references/concurrency-and-error-handling.md",
        "references/framework-and-platform-boundaries.md",
        "references/performance-and-memory.md",
        "references/testing.md",
        "references/types-and-api-design.md",
    }


def test_synced_swift_skill_is_searchable(temp_db: HubDatabase) -> None:
    """Verify bundled sync makes Swift discoverable through skill search."""
    result = sync_bundled_skills(temp_db)
    assert result["success"] is True

    manager = SkillManager(temp_db)
    skill = manager.get_by_name("swift")
    assert skill is not None
    assert skill.source == "installed"
    assert skill.source_type == "filesystem"

    search = SkillSearch(db=temp_db, config=SearchConfig(mode="keyword"))
    search.index_skills([skill])

    results = search.search("swift swiftpm xcode actors sendable swift-testing swiftlint", top_k=3)
    assert results
    assert results[0].skill_name == "swift"
