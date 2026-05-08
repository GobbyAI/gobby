"""Red tests for the interactive /gobby qa skill contract."""

from __future__ import annotations

import pytest

from tests.skills.interactive_skill_helpers import (
    assert_interactive_skill_contract,
    read_skill,
)

pytestmark = pytest.mark.unit

SKILL_PATH = "src/gobby/install/shared/skills/qa/SKILL.md"
WORKFLOW_PATH = "src/gobby/install/shared/workflows/qa.yaml"


def test_id_opt_in_present() -> None:
    body = read_skill(SKILL_PATH)

    assert_interactive_skill_contract(
        body,
        name="qa",
        command="/gobby qa",
        agent="qa-reviewer",
        workflow_path=WORKFLOW_PATH,
    )
    assert "approve / reject / escalate" in body
    assert "approve_review" in body
    assert "reject_review" in body
    assert "escalate_task" in body
    assert "read-only" in body.lower()
