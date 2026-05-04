"""Contract tests for the bundled verification-before-completion skill."""

from __future__ import annotations

import pytest
import yaml

from gobby.skills.loader import SkillLoader

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def skill_dir(repo_root):
    return repo_root / "src/gobby/install/shared/skills/verification-before-completion"


@pytest.fixture(scope="module")
def skills_root(repo_root):
    return repo_root / "src/gobby/install/shared/skills"


def _frontmatter(skill_dir) -> dict:
    body = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    header = body.split("---", 2)[1]
    data = yaml.safe_load(header)
    assert isinstance(data, dict)
    return data


def test_metadata_is_discoverable_and_enabled_by_default(skill_dir) -> None:
    frontmatter = _frontmatter(skill_dir)
    skill = SkillLoader().load_skill(skill_dir)

    assert frontmatter["name"] == "verification-before-completion"
    assert frontmatter["description"].startswith("Use when")
    assert frontmatter["category"] == "core"
    assert skill.name == "verification-before-completion"
    assert skill.get_category() == "core"


def test_bundled_directory_discovery_finds_skill(skills_root) -> None:
    skills = SkillLoader().load_directory(skills_root)

    assert "verification-before-completion" in {skill.name for skill in skills}


def test_skill_requires_fresh_evidence_before_success_claims(skill_dir) -> None:
    body = (skill_dir / "SKILL.md").read_text(encoding="utf-8")

    assert "fresh evidence first, success claim second" in body
    assert "close_task" in body
    assert "submit_for_review" in body
    assert "approve_review" in body
    assert "commit_sha" in body
    assert "require-error-triage-before-status" in body
    assert "Run the command. Read the output. Then claim the result." in body
