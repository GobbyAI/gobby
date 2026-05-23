"""Contract tests for the bundled CodeRabbit skill."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.skills.loader import SkillLoader
from gobby.skills.parser import parse_skill_file

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_DIR = REPO_ROOT / "src/gobby/install/shared/skills/coderabbit"
SKILLS_ROOT = REPO_ROOT / "src/gobby/install/shared/skills"


def _body() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_coderabbit_skill_parses_and_is_discoverable() -> None:
    """Verify the CodeRabbit SKILL.md parses and is discoverable by the skill loader."""
    parsed = parse_skill_file(SKILL_DIR / "SKILL.md")
    skills = SkillLoader().load_directory(SKILLS_ROOT)

    assert parsed.name == "coderabbit"
    assert parsed.description.startswith("Use when")
    assert "$gobby coderabbit" in parsed.description
    assert "coderabbit" in {skill.name for skill in skills}


def test_coderabbit_skill_requires_verification_before_fixes() -> None:
    """Verify the skill requires agents to inspect current code before applying fixes."""
    body = _body()

    assert "Verify each item against current code" in body
    assert "before changing files" in body
    assert "Inspect current code for each finding before deciding" in body
    assert "Fix only findings that still apply" in body
    assert "Include nits" in body


def test_coderabbit_skill_requires_plan_mode_triage_before_edits() -> None:
    """Verify CodeRabbit triage happens in Plan Mode before implementation edits."""
    body = _body()

    assert "## Plan Mode Gate" in body
    assert "If it is not, you MUST enter native Plan Mode" in body
    assert (
        "before reading\nreports, verifying findings, creating or claiming tasks, or editing files"
        in body
    )
    assert "session-level planning is the\nassistant's internal planning posture" in body
    assert "`EnterPlanMode`" in body
    assert "wait for plan approval before continuing" in body
    assert "Plan Mode triage is read-only" in body
    assert "before the first edit" in body


def test_coderabbit_skill_documents_no_fix_decisions() -> None:
    """Verify the skill requires documented no-fix decisions for stale or invalid findings."""
    body = _body()

    assert "`no-fix`" in body
    assert "Every `no-fix` decision needs a short reason" in body
    assert "Do not silently drop stale comments" in body
    assert "current code does not match the finding" in body


def test_coderabbit_skill_handles_reports_and_cleanup() -> None:
    """Verify the skill covers CodeRabbit report ingestion, CLI failures, and cleanup."""
    body = _body()

    assert "./reports/coderabbit-*.md" in body
    assert "CodeRabbit CLI failure" in body
    assert "Too many files" in body
    assert "Delete processed `./reports/coderabbit-*.md` files" in body
    assert "Leave unrelated report artifacts alone" in body


def test_coderabbit_skill_requires_validation_commit_and_task_close() -> None:
    """Verify the skill requires validation, a task-referenced commit, and task closure."""
    body = _body()

    assert "REQUIRED SKILL: verification-before-completion" in body
    assert "Run focused validation" in body
    assert "Commit with the task ref" in body
    assert "close the task with `commit_sha`" in body
