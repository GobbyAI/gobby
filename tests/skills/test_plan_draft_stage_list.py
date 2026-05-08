"""Plan-draft stage-list contract tests."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from gobby.config.build import SKIPPABLE_STAGES, SkippableStage

pytestmark = pytest.mark.unit

SKILL_PATH = Path("src/gobby/install/shared/skills/plan-draft/SKILL.md")
DROPPED_STAGES = frozenset({"adversarial_review", "expansion_qa", "code_review_qa"})


def _stage_section(body: str) -> str:
    match = re.search(
        r"### Canonical Build Stages\n(?P<section>.*?)(?:\n### |\n## |\Z)",
        body,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group("section")


def _documented_stages() -> tuple[str, ...]:
    body = SKILL_PATH.read_text(encoding="utf-8")
    section = _stage_section(body)
    return tuple(re.findall(r"^\d+\.\s+`([^`]+)`\s*$", section, flags=re.MULTILINE))


def test_matches_registry() -> None:
    documented = _documented_stages()
    registry_order = tuple(get_args(SkippableStage))

    assert documented == registry_order
    assert set(documented) == SKIPPABLE_STAGES


def test_dropped_stages_absent() -> None:
    body = SKILL_PATH.read_text(encoding="utf-8")
    documented = set(_documented_stages())

    assert documented.isdisjoint(DROPPED_STAGES)
    for stage_name in DROPPED_STAGES:
        assert stage_name not in body
