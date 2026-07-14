"""Contract tests for the bundled Lua language skill."""

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
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/lua"


def test_lua_skill_parses_with_references_and_provenance() -> None:
    """Verify Lua metadata, references, and license-safe source notes."""
    parsed = SkillLoader().load_skill(SKILL_DIR, validate=False)

    assert parsed.name == "lua"
    assert parsed.version == "1.0.0"
    assert parsed.get_category() == "development"
    assert parsed.triggers is not None
    assert {
        "lua",
        "luac",
        "luarocks",
        "busted",
        "stylua",
        "luacheck",
        "lua-language-server",
    }.issubset(parsed.triggers)
    assert (
        'get_skill_file(name="lua", path="references/configuration-and-modules.md")'
        in parsed.content
    )
    assert (
        'get_skill_file(name="lua", path="references/embedding-and-platform-boundaries.md")'
        in parsed.content
    )

    frontmatter, _ = parse_frontmatter((SKILL_DIR / "SKILL.md").read_text())
    sources = frontmatter.get("sources")
    assert isinstance(sources, list)
    source_text = " ".join(str(source) for source in sources)
    assert "https://www.lua.org/manual/5.5/" in source_text
    assert "https://www.lua.org/manual/5.4/" in source_text
    assert "ar4mirez/samuel-claude-skills" in source_text
    assert "repository was unavailable" in source_text
    assert "no license could be verified" in source_text
    assert "no text or code copied" in source_text

    assert parsed.loaded_files is not None
    reference_paths = {file.path for file in parsed.loaded_files if file.file_type == "reference"}
    assert reference_paths == {
        "references/configuration-and-modules.md",
        "references/coroutines-and-concurrency.md",
        "references/embedding-and-platform-boundaries.md",
        "references/errors-and-resources.md",
        "references/performance-and-security.md",
        "references/tables-types-and-metatables.md",
        "references/testing-and-tooling.md",
    }

    table_guidance = (SKILL_DIR / "references/tables-types-and-metatables.md").read_text()
    assert "caller-controlled metamethods" in table_guidance
    assert "Detect cycles and preserve or reject aliasing" in table_guidance


def test_synced_lua_skill_is_searchable(temp_db: HubDatabase) -> None:
    """Verify bundled sync makes Lua discoverable through skill search."""
    result = sync_bundled_skills(temp_db)
    assert result["success"] is True

    manager = SkillManager(temp_db)
    skill = manager.get_by_name("lua")
    assert skill is not None
    assert skill.source == "installed"
    assert skill.source_type == "filesystem"

    search = SkillSearch(db=temp_db, config=SearchConfig(mode="keyword"))
    search.index_skills([skill])

    results = search.search(
        "lua luac luarocks tables metatables coroutines embedding busted stylua luacheck",
        top_k=3,
    )
    assert results
    assert results[0].skill_name == "lua"
