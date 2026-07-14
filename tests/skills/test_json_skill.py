"""Tests for the bundled JSON language skill."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.skills.loader import SkillLoader

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/json"


def test_json_skill_parses_with_references() -> None:
    """Verify the bundled JSON skill has expected metadata and reference files."""
    parsed = SkillLoader().load_skill(SKILL_DIR, validate=False)

    assert parsed.name == "json"
    assert parsed.version == "1.1.0"
    assert parsed.get_category() == "development"
    assert parsed.triggers is not None
    assert {
        "json",
        "jsonc",
        "json5",
        "json-schema",
        "package-json",
        "jq",
        "prettier",
        "biome",
        "tsconfig",
    }.issubset(parsed.triggers)
    assert 'get_skill_file(name="json", path="references/configuration.md")' in parsed.content
    assert (
        'get_skill_file(name="json", path="references/schema-and-validation.md")' in parsed.content
    )
    assert (
        'get_skill_file(name="json", path="references/parsing-and-serialization.md")'
        in parsed.content
    )
    assert (
        'get_skill_file(name="json", path="references/security-and-secrets.md")' in parsed.content
    )

    assert parsed.loaded_files is not None
    reference_paths = {file.path for file in parsed.loaded_files if file.file_type == "reference"}
    assert reference_paths == {
        "references/configuration.md",
        "references/parsing-and-serialization.md",
        "references/schema-and-validation.md",
        "references/security-and-secrets.md",
        "references/syntax-and-data-model.md",
        "references/testing.md",
    }
