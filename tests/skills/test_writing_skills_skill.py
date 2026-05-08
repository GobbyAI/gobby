"""Contract tests for the bundled writing-skills authoring skill."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.skills.loader import SkillLoader

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/writing-skills"
SKILLS_ROOT = REPO_ROOT / "src/gobby/install/shared/skills"


def _body() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter() -> dict:
    header = _body().split("---", 2)[1]
    data = yaml.safe_load(header)
    assert isinstance(data, dict)
    return data


def test_metadata_is_discoverable_and_authoring_category() -> None:
    frontmatter = _frontmatter()
    skill = SkillLoader().load_skill(SKILL_DIR)

    assert frontmatter["name"] == "writing-skills"
    assert frontmatter["description"].startswith("Use when")
    assert frontmatter["category"] == "authoring"
    assert skill.name == "writing-skills"
    assert skill.get_category() == "authoring"


def test_bundled_directory_discovery_finds_writing_skills() -> None:
    skills = SkillLoader().load_directory(SKILLS_ROOT)

    assert "writing-skills" in {skill.name for skill in skills}


def test_skill_is_adapted_to_gobby_skill_tdd() -> None:
    body = _body()

    assert "no skill without a failing scenario first" in body
    assert "gobby-skills" in body
    assert "tests/skills/scenarios/<skill-name>/" in body
    assert "uv run pytest tests/skills/ -m skill_tdd" in body
    assert "src/gobby/install/shared/skills/<skill-name>/SKILL.md" in body
    assert "do not rely on native CLI skill tools" in body
