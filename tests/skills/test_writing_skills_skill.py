"""Skill authoring remains discoverable and behaviorally verified."""

from pathlib import Path

import pytest

from gobby.skills.capability_catalog import load_capability_catalog
from gobby.skills.loader import SkillLoader

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/skills"
REFERENCE = ROOT / "gobby/references/skills/authoring.md"


def test_metadata_is_discoverable_and_authoring_category() -> None:
    catalog = load_capability_catalog()
    capability = next(item for item in catalog.capabilities if item.name == "skills")
    authoring = next(topic for topic in capability.topics if topic.name == "authoring")
    assert authoring.description and authoring.when
    assert catalog.folded_skills["writing-skills"] == "gobby:references/skills/authoring.md"


def test_bundled_directory_discovery_finds_writing_skills() -> None:
    names = {skill.name for skill in SkillLoader().load_directory(ROOT)}
    assert "gobby" in names
    assert "writing-skills" not in names
    assert REFERENCE.is_file()


def test_skill_is_adapted_to_gobby_skill_tdd() -> None:
    body = " ".join(REFERENCE.read_text().split())
    for expected in (
        "Before a behavior-changing skill, add a pressure scenario",
        "tests/skills/scenarios/<name>/",
        "excluded-skill failure",
        "loaded-skill improvement",
        "gobby-skills",
        "native CLI skill tools do not record Gobby loads",
        "src/gobby/install/shared/skills/<name>/",
        "new rationalizations",
        "rerun it",
        "migration alone must not weaken behavior",
    ):
        assert expected in body


def test_skill_requires_semantic_bundled_decomposition() -> None:
    body = " ".join(REFERENCE.read_text().split())
    for expected in (
        "skills.bundled_max_content_size",
        "default 15000",
        "character and UTF-8 byte",
        "Split by semantic topic with exact loading conditions",
        "at most three references",
        "preserve artifacts and validators",
        "recovery",
        "isolated state",
    ):
        assert expected in body
