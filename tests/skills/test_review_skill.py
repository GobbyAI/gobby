"""Red tests for the interactive /gobby review skill contract."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = REPO_ROOT / "src/gobby/install/shared/skills/review/SKILL.md"


def _read_skill() -> str:
    assert SKILL_PATH.exists(), f"{SKILL_PATH} should exist"
    return SKILL_PATH.read_text(encoding="utf-8")


def _frontmatter(body: str) -> dict[str, Any]:
    match = re.match(r"^---\n(?P<yaml>.*?)\n---\n", body, flags=re.DOTALL)
    assert match is not None, "skill should use YAML frontmatter"
    data = yaml.safe_load(match.group("yaml"))
    assert isinstance(data, dict)
    return data


def test_id_opt_in_present() -> None:
    body = _read_skill()
    frontmatter = _frontmatter(body)

    assert frontmatter["name"] == "review"
    assert frontmatter["metadata"]["gobby"]["audience"] == "interactive"
    assert frontmatter["metadata"]["gobby"]["depth"] == 0
    assert "/gobby review" in body
    assert "I/D" in body
    assert re.search(r"\bI\)\s*Interactive\b", body) is not None
    assert re.search(r"\bD\)\s*Delegated\b", body) is not None
    assert 'value="interactive" | "delegated"' in body
    assert "holistic-reviewer" in body
    assert "approve / reject / escalate" in body
    assert "mark_task_review_approved" in body
    assert "mark_task_review_rejected" in body
    assert "escalate_task" in body
    assert "src/gobby/install/shared/workflows/review.yaml" in body
