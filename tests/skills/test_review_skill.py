"""Epic review routes interactive and delegated work through current references."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
REFERENCES = Path("src/gobby/install/shared/skills/gobby/references/review")


def test_id_opt_in_present() -> None:
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
    for term in ("approve_review", "reject_review", "escalate_task", "cited_subtasks"):
        assert term in outcomes
