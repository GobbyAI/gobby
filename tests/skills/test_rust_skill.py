"""Contract tests for the bundled Rust language skill."""

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
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/rust"


def test_rust_skill_parses_with_references() -> None:
    """Verify the bundled Rust skill has expected metadata and reference files."""
    parsed = SkillLoader().load_skill(SKILL_DIR, validate=False)

    assert parsed.name == "rust"
    assert parsed.version == "1.2.0"
    assert parsed.get_category() == "development"
    assert parsed.triggers is not None
    assert {
        "rust",
        "cargo",
        "cargo.toml",
        "rust-toolchain",
        "clippy",
        "rustfmt",
        "tokio",
        "async rust",
        "borrow checker",
    }.issubset(parsed.triggers)
    assert 'get_skill_file(name="rust", path="references/configuration.md")' in parsed.content
    assert 'get_skill_file(name="rust", path="references/ownership.md")' in parsed.content
    assert 'get_skill_file(name="rust", path="references/types.md")' in parsed.content
    assert 'get_skill_file(name="rust", path="references/error-handling.md")' in parsed.content
    assert 'get_skill_file(name="rust", path="references/testing.md")' in parsed.content

    assert parsed.loaded_files is not None
    reference_paths = {file.path for file in parsed.loaded_files if file.file_type == "reference"}
    assert reference_paths == {
        "references/async.md",
        "references/configuration.md",
        "references/error-handling.md",
        "references/ownership.md",
        "references/performance.md",
        "references/testing.md",
        "references/types.md",
    }


def test_synced_rust_skill_is_searchable(temp_db: HubDatabase) -> None:
    """Verify bundled sync makes Rust discoverable through skill search."""
    result = sync_bundled_skills(temp_db)
    assert result["success"] is True

    manager = SkillManager(temp_db)
    skill = manager.get_by_name("rust")
    assert skill is not None
    assert skill.source == "installed"
    assert skill.source_type == "filesystem"

    search = SkillSearch(db=temp_db, config=SearchConfig(mode="keyword"))
    search.index_skills([skill])

    results = search.search(
        "rust cargo rust-toolchain clippy rustfmt tokio ownership borrow checker",
        top_k=3,
    )
    assert results
    assert results[0].skill_name == "rust"
