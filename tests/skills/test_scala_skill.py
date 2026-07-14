"""Contract tests for the bundled Scala language skill."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.search.models import SearchConfig
from gobby.skills.loader import SkillLoader
from gobby.skills.manager import SkillManager
from gobby.skills.parser import parse_frontmatter
from gobby.skills.search import SkillSearch
from gobby.skills.sync import sync_bundled_skills
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/scala"


def test_scala_skill_parses_with_references_and_provenance() -> None:
    """Verify Scala metadata, references, and license-safe source notes."""
    parsed = SkillLoader().load_skill(SKILL_DIR, validate=False)

    assert parsed.name == "scala"
    assert parsed.version == "1.0.0"
    assert parsed.get_category() == "development"
    assert parsed.triggers is not None
    assert {"scala", "scalac", "sbt", "scala-cli", "scalafmt", "metals"}.issubset(parsed.triggers)
    assert 'get_skill_file(name="scala", path="references/configuration.md")' in parsed.content
    assert (
        'get_skill_file(name="scala", '
        'path="references/types-and-contextual-abstractions.md")' in parsed.content
    )

    frontmatter, _ = parse_frontmatter((SKILL_DIR / "SKILL.md").read_text())
    sources = frontmatter.get("sources")
    assert isinstance(sources, list)
    source_text = " ".join(str(source) for source in sources)
    assert "https://docs.scala-lang.org/scala3/reference/" in source_text
    assert "EtaCassiopeia/claude-skills" in source_text
    assert "no repository license declared" in source_text
    assert "no text or code copied" in source_text

    assert parsed.loaded_files is not None
    reference_paths = {file.path for file in parsed.loaded_files if file.file_type == "reference"}
    assert reference_paths == {
        "references/configuration.md",
        "references/effects-errors-and-resources.md",
        "references/framework-and-platform-boundaries.md",
        "references/performance-and-concurrency.md",
        "references/testing.md",
        "references/types-and-contextual-abstractions.md",
    }


def test_synced_scala_skill_is_searchable(temp_db: HubDatabase) -> None:
    """Verify bundled sync makes Scala discoverable through skill search."""
    result = sync_bundled_skills(temp_db)
    assert result["success"] is True

    manager = SkillManager(temp_db)
    skill = manager.get_by_name("scala")
    assert skill is not None
    assert skill.source == "installed"
    assert skill.source_type == "filesystem"

    search = SkillSearch(db=temp_db, config=SearchConfig(mode="keyword"))
    search.index_skills([skill])

    results = search.search(
        "scala scalac sbt scala-cli opaque types given enum scalafmt metals", top_k=3
    )
    assert results
    assert results[0].skill_name == "scala"
