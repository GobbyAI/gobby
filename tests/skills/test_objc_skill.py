"""Contract tests for the bundled Objective-C language skill."""

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
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/objc"


def test_objc_skill_parses_with_references_and_sources() -> None:
    """Verify Objective-C metadata, references, and source attribution."""
    parsed = SkillLoader().load_skill(SKILL_DIR, validate=False)

    assert parsed.name == "objc"
    assert parsed.version == "1.0.0"
    assert parsed.get_category() == "development"
    assert parsed.triggers is not None
    assert {
        "objective-c",
        "objective-c++",
        "objc",
        "clang",
        "arc",
        "foundation",
        "blocks",
        "swift-interop",
    }.issubset(parsed.triggers)
    assert (
        'get_skill_file(name="objc", path="references/configuration-and-language-modes.md")'
        in parsed.content
    )
    assert (
        'get_skill_file(name="objc", path="references/swift-and-c-family-interop.md")'
        in parsed.content
    )

    frontmatter, _ = parse_frontmatter((SKILL_DIR / "SKILL.md").read_text())
    sources = frontmatter.get("sources")
    assert isinstance(sources, list)
    source_text = " ".join(str(source) for source in sources)
    assert "clang.llvm.org/docs/AutomaticReferenceCounting.html" in source_text
    assert (
        "developer.apple.com/library/archive/documentation/Cocoa/Conceptual/ProgrammingWithObjectiveC"
        in source_text
    )
    assert "developer.apple.com/documentation/swift/importing-objective-c-into-swift" in source_text
    assert "G1Joshi/Agent-Skills" in source_text

    assert parsed.loaded_files is not None
    reference_paths = {file.path for file in parsed.loaded_files if file.file_type == "reference"}
    assert reference_paths == {
        "references/blocks-and-concurrency.md",
        "references/configuration-and-language-modes.md",
        "references/foundation-and-api-design.md",
        "references/ownership-and-lifetimes.md",
        "references/runtime-performance-and-security.md",
        "references/swift-and-c-family-interop.md",
        "references/testing-and-tooling.md",
    }

    ownership = (SKILL_DIR / "references/ownership-and-lifetimes.md").read_text()
    assert "Determine ARC per target and source file" in ownership
    blocks = (SKILL_DIR / "references/blocks-and-concurrency.md").read_text()
    assert "A copied block retains captured Objective-C objects" in blocks
    assert "strong reference cycle" in blocks
    interop = (SKILL_DIR / "references/swift-and-c-family-interop.md").read_text()
    assert "NS_ASSUME_NONNULL_BEGIN" in interop
    assert "lightweight generics" in interop


def test_synced_objc_skill_is_searchable(temp_db: HubDatabase) -> None:
    """Verify bundled sync makes Objective-C discoverable through skill search."""
    result = sync_bundled_skills(temp_db)
    assert result["success"] is True

    manager = SkillManager(temp_db)
    skill = manager.get_by_name("objc")
    assert skill is not None
    assert skill.source == "installed"
    assert skill.source_type == "filesystem"

    search = SkillSearch(db=temp_db, config=SearchConfig(mode="keyword"))
    search.index_skills([skill])

    results = search.search(
        "objective-c objc clang arc mrc foundation blocks swift interop xcode",
        top_k=3,
    )
    assert results
    assert results[0].skill_name == "objc"
