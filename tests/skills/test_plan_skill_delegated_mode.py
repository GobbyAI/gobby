"""Content tests for taskless /gobby plan skill behavior."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SKILL_PATH = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/skills/plan/SKILL.md"


@pytest.fixture(scope="module")
def body() -> str:
    return SKILL_PATH.read_text()


def test_plan_is_artifact_first_and_taskless(body: str) -> None:
    lowered = body.lower()

    assert "artifact-first" in lowered
    assert "does not create a planning" in lowered
    assert "review-anchor task" in lowered
    assert "per-round review tasks" in lowered
    assert "do not create or claim tasks" in lowered
    assert "do not create review anchors" in lowered


def test_plan_loads_draft_methodology_and_validates_before_review(body: str) -> None:
    assert 'get_skill(name="plan-draft")' in body
    assert "uv run gobby plans validate <plan-file>" in body
    assert "Ask the user to approve the draft for adversarial review" in body


def test_review_spawn_uses_taskless_adversary_without_task_id(body: str) -> None:
    assert "plan-adversary-taskless" in body
    assert "without `task_id`" in body
    assert 'isolation="none"' in body
    assert "artifact_path" in body
    assert "round_number" in body
    assert "max_review_rounds" in body


def test_review_history_uses_v1_changelog_verification_entries(body: str) -> None:
    assert "## V1 Plan Changelog" in body
    assert "`kind: verification`" in body
    for field in (
        "reviewer_run",
        "reviewer_session",
        "verdict: approved | needs_review | needs_requirements",
        "findings",
        "resolution_notes",
    ):
        assert field in body
    assert "Keep prior rounds" in body


def test_build_handoff_uses_manifest_and_seed_flags(body: str) -> None:
    assert "## M1 Task Manifest" in body
    assert "uv run gobby plans validate <plan-file> --mode expansion" in body
    assert "uv run gobby build <plan-file>" in body
    assert "--planning-seed-state approved" in body
    assert "--completed-plan-review-rounds <N>" in body
    assert "planning_seed_state=drafted" in body
    assert "planning_seed_state=needs_review" in body
    assert "planning_seed_state=approved" in body


def test_old_anchor_workflow_is_absent(body: str) -> None:
    for forbidden in (
        "active_anchor_id",
        'task_type="review_anchor"',
        "submit_for_review(task_id=anchor.id",
        "start_stage(task_id=anchor.id",
    ):
        assert forbidden not in body
