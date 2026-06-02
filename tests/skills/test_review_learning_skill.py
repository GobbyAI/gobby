"""Contract tests for the bundled review-learning skill and producer hooks."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.skills.loader import SkillLoader
from gobby.skills.parser import parse_skill_file

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/review-learning"
SKILLS_ROOT = REPO_ROOT / "src/gobby/install/shared/skills"
WORKFLOWS = REPO_ROOT / "src/gobby/install/shared/workflows/agents"


def _body() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_review_learning_skill_parses_and_is_discoverable() -> None:
    parsed = parse_skill_file(SKILL_DIR / "SKILL.md")
    skills = SkillLoader().load_directory(SKILLS_ROOT)

    assert parsed.name == "review-learning"
    assert parsed.description.startswith("Use when")
    assert "review-learning" in {skill.name for skill in skills}


def test_review_learning_skill_documents_tool_contract() -> None:
    body = _body()

    assert "gobby-review-learning" in body
    assert "recall_review_context" in body
    assert "record_review_lesson" in body
    assert "Relevant memory/lesson" in body
    assert "pattern_id" in body
    assert "principle" in body
    assert "root_cause" in body
    assert "prevention" in body
    assert "query_hints" in body
    assert "gcode search" in body
    assert "gcode grep" in body


def test_review_learning_skill_documents_record_skip_and_ladder_rules() -> None:
    body = _body()

    assert "A raw failure with no verified fix must not" in body
    assert "`stale` or `invalid`: skip recording" in body
    assert "`confirmed`, second occurrence: `test`" in body
    assert "`confirmed`, third or later occurrence: `validation`" in body
    assert "`no-fix-policy`, second or later occurrence" in body
    assert "`checklist` or `tool-config`" in body
    assert "The task is not the guardrail" in body


def test_review_producer_hooks_reference_review_learning() -> None:
    code_reviewer = (SKILLS_ROOT / "code-reviewer/SKILL.md").read_text(encoding="utf-8")
    holistic = (SKILLS_ROOT / "holistic-review/SKILL.md").read_text(encoding="utf-8")
    qa_reviewer = (WORKFLOWS / "qa-reviewer.yaml").read_text(encoding="utf-8")
    nightly_linter = (WORKFLOWS / "nightly-linter.yaml").read_text(encoding="utf-8")
    nightly_test = (WORKFLOWS / "nightly-test-fixer.yaml").read_text(encoding="utf-8")

    assert "REQUIRED SKILL: review-learning" in code_reviewer
    assert "recall_review_context" in code_reviewer
    assert "source_kind=agent_review" in code_reviewer
    assert "REQUIRED SKILL: review-learning" in holistic
    assert "source_kind=qa_rejection" in holistic
    assert "review-learning" in qa_reviewer
    assert "record_review_lesson" in qa_reviewer
    assert "source_kind=static_analysis" in nightly_linter
    assert "Do not record raw report failures" in nightly_linter
    assert "source_kind=test_failure" in nightly_test
    assert "Do not record raw failures" in nightly_test
