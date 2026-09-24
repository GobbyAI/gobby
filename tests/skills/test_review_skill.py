"""Epic review routes interactive and delegated work through current references."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
REFERENCES = Path("src/gobby/install/shared/skills/gobby/references/review")


def test_epic_review_references_pin_routing_and_verdict_mapping() -> None:
    epic = " ".join((REFERENCES / "epic.md").read_text().split())
    outcomes = " ".join((REFERENCES / "outcomes.md").read_text().split())
    for term in (
        "Ask for a missing target",
        "`interactive` or `delegated`",
        'apply_persona(agent="epic-reviewer")',
        'run_pipeline(name="review"',
        "installed rows and effective project overrides",
        "launcher does not issue a second one",
        "Never call `close_task` from epic review",
    ):
        assert term in epic
    for term in (
        'Approve → `gobby-tasks-ops:complete_stage` with `stage_name="epic_qa"`',
        "Request changes → `gobby-tasks-ops:fail_stage` with the verdict in `reason` and "
        "`cited_subtasks`",
        "Needs discussion → `gobby-tasks:escalate_task` with a `needs_human:` reason",
        "Do not substitute `approve_review`/`reject_review` for an in-progress epic QA verdict",
    ):
        assert term in outcomes
    assert "record_review_lesson" not in outcomes
