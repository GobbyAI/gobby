"""Contract tests for the bundled verification-before-completion skill."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.skills.loader import SkillLoader

pytestmark = pytest.mark.unit


def _find_repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / "pyproject.toml").exists() or (candidate / ".git").exists():
            return candidate
    msg = f"Could not find repository root from {start}"
    raise RuntimeError(msg)


REPO_ROOT = _find_repo_root(Path(__file__).resolve())
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/verification-before-completion"
SKILLS_ROOT = REPO_ROOT / "src/gobby/install/shared/skills"


def _frontmatter() -> dict:
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    header = body.split("---", 2)[1]
    data = yaml.safe_load(header)
    assert isinstance(data, dict)
    return data


def test_metadata_is_discoverable_and_enabled_by_default() -> None:
    frontmatter = _frontmatter()
    skill = SkillLoader().load_skill(SKILL_DIR)

    assert frontmatter["name"] == "verification-before-completion"
    assert frontmatter["description"].startswith("Use when")
    assert frontmatter["category"] == "core"
    assert skill.name == "verification-before-completion"
    assert skill.get_category() == "core"


def test_bundled_directory_discovery_finds_skill() -> None:
    skills = SkillLoader().load_directory(SKILLS_ROOT)

    assert "verification-before-completion" in {skill.name for skill in skills}


def test_skill_requires_fresh_evidence_before_success_claims() -> None:
    body = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert "fresh evidence first, success claim second" in body
    assert "close_task" in body
    assert "submit_for_review" in body
    assert "approve_review" in body
    assert "commit_sha" in body
    assert "require-error-triage-before-status" in body
    assert "Run the command. Read the output. Then claim the result." in body
