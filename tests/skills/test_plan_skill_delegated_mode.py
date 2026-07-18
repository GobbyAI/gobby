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


def test_plan_compacts_after_every_review_agent_launch(body: str) -> None:
    enhancer_launch = body.index("Spawn `plan-enhancer-taskless`")
    enhancer_wait = body.index("Wait for the run completion message", enhancer_launch)
    enhancer_handoff = body[enhancer_launch:enhancer_wait]
    assert "gobby-sessions:compact_self" in enhancer_handoff
    assert "every enhancement round" in enhancer_handoff

    adversary_launch = body.index("spawn `plan-adversary-taskless`")
    adversary_wait = body.index("Wait for the adversary run completion message", adversary_launch)
    adversary_handoff = body[adversary_launch:adversary_wait]
    assert "gobby-sessions:compact_self" in adversary_handoff
    assert "every adversarial review round" in adversary_handoff


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


def test_enhancement_phase_precedes_adversary_gate(body: str) -> None:
    # Step 4.5: constructive enhancement runs after approval, before the adversary.
    assert "Step 4.5" in body
    assert "plan-enhancer-taskless" in body
    assert "max_enhancement_rounds" in body
    lowered = body.lower()
    # Human is the scope gate; enhancer never gates.
    assert "present-and-stop" in lowered
    assert "scope gate" in lowered
    # Advisory: accepted suggestions only, stop on convergence/decline/cap.
    assert "accepted" in lowered
    assert "converged" in lowered
    assert "never gate" in lowered


def test_enhancement_changelog_uses_kind_enhancement(body: str) -> None:
    assert "`kind: enhancement`" in body
    assert "enhancer_run" in body
    assert "suggestions_presented" in body
    # Round entries are bold labels; an actual `### Round` heading (line-start)
    # fails plan validation, so the changelog examples must never use one.
    assert "\n### Round" not in body
    assert "**Round <N>**" in body


def test_old_anchor_workflow_is_absent(body: str) -> None:
    for forbidden in (
        "active_anchor_id",
        'task_type="review_anchor"',
        "submit_for_review(task_id=anchor.id",
        "start_stage(task_id=anchor.id",
    ):
        assert forbidden not in body
