"""Red tests for the interactive /gobby review skill contract."""

from __future__ import annotations

import pytest

from tests.skills.interactive_skill_helpers import (
    assert_interactive_skill_contract,
    read_skill,
)

pytestmark = pytest.mark.unit

SKILL_PATH = "src/gobby/install/shared/skills/review/SKILL.md"
WORKFLOW_PATH = "src/gobby/install/shared/workflows/review.yaml"


def test_id_opt_in_present() -> None:
    body = read_skill(SKILL_PATH)

    assert_interactive_skill_contract(
        body,
        name="review",
        command="/gobby review",
        agent="holistic-reviewer",
        workflow_path=WORKFLOW_PATH,
    )
    assert "approve / reject / escalate" in body
    assert "mark_task_review_approved" in body
    assert "mark_task_review_rejected" in body
    assert "escalate_task" in body
