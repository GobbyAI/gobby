"""Contract tests for the bundled Python language skill."""

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
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/python"
DEVELOPMENT_DISCIPLINE_FILE = (
    REPO_ROOT / "src/gobby/install/shared/skills/development-discipline/SKILL.md"
)


def test_python_skill_parses_with_references() -> None:
    """Verify the bundled Python skill has expected metadata and reference files."""
    parsed = SkillLoader().load_skill(SKILL_DIR, validate=False)

    assert parsed.name == "python"
    assert parsed.version == "1.2.1"
    assert parsed.get_category() == "development"
    assert parsed.triggers is not None
    assert {
        "python",
        "py",
        "pyi",
        "pyproject.toml",
        "uv",
        "ruff",
        "mypy",
        "pytest",
        "tox",
        "nox",
        "typing",
        "asyncio",
    }.issubset(parsed.triggers)
    assert 'get_skill_file(name="python", path="references/configuration.md")' in parsed.content
    assert 'get_skill_file(name="python", path="references/types.md")' in parsed.content
    assert 'get_skill_file(name="python", path="references/error-handling.md")' in (parsed.content)
    assert 'get_skill_file(name="python", path="references/testing.md")' in parsed.content

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


def test_python_skill_prohibits_suppressions_and_routes_to_typed_boundaries() -> None:
    """Require root-cause typing repairs and the repository ratchet."""
    content = SkillLoader().load_skill(SKILL_DIR, validate=False).content

    assert "Do not add `# noqa` or `# type: ignore`" in content
    assert "typed adapter" in content
    assert "local stub" in content
    assert "explicit facade exports" in content.lower()
    assert "gobby test-types suppressions" in content
    assert "last resort" not in content


def test_development_discipline_prohibits_python_suppressions() -> None:
    content = DEVELOPMENT_DISCIPLINE_FILE.read_text(encoding="utf-8")

    assert "never add `# type: ignore` or `# noqa`" in content
    assert "typed adapter" in content
    assert "local stub" in content
    assert "gobby test-types suppressions" in content


def test_synced_python_skill_is_searchable(temp_db: HubDatabase) -> None:
    """Verify bundled sync makes Python discoverable through skill search."""
    result = sync_bundled_skills(temp_db)
    assert result["success"] is True

    manager = SkillManager(temp_db)
    skill = manager.get_by_name("python")
    assert skill is not None
    assert skill.source == "installed"
    assert skill.source_type == "filesystem"

    search = SkillSearch(db=temp_db, config=SearchConfig(mode="keyword"))
    search.index_skills([skill])

    results = search.search("python typing pytest ruff asyncio", top_k=3)
    assert results
    assert results[0].skill_name == "python"
