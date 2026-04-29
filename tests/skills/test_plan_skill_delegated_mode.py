"""Content tests for delegated mode in /gobby plan skill."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SKILL_PATH = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/skills/plan/SKILL.md"


@pytest.fixture(scope="module")
def body() -> str:
    return SKILL_PATH.read_text()


def test_step_1a_presents_yn_opt_in(body: str) -> None:
    assert "## Step 1: Adversarial Opt-in" in body
    assert "Do you want adversarial review on this plan?" in body
    assert "Y) Yes" in body
    assert "N) No / Plain" in body
    assert 'value="plain"' in body
    assert 'name="plan_review_requested"' in body


def test_step_6b_presents_interactive_delegated(body: str) -> None:
    assert "## Step 6b: Adversary Mode Selection" in body
    assert "I) Interactive" in body
    assert "D) Delegated" in body
    assert 'value="adversarial" | "delegated"' in body
    assert "How many adversary rounds?" in body


def test_pre_set_plan_review_mode_skips_menu(body: str) -> None:
    assert "If `plan_review_mode` is already set" in body
    assert '"adversarial"' in body
    assert '"delegated"' in body
    assert '"plain"' in body


def test_step_7_requires_artifact_path(body: str) -> None:
    assert "## Step 7: Review Loop" in body
    assert "### 7.0. Artifact precondition" in body
    assert "artifact_path is missing" in body


def test_delegated_mode_skips_per_round_plan_mode_reentry(body: str) -> None:
    assert 'If `plan_review_mode == "delegated"`' in body
    assert "without re-entering plan mode" in body
    assert "Do not interrupt" in body


def test_step_7_3a_pre_flight_factcheck_exists(body: str) -> None:
    """Every round dispatches a mid-tier subagent to mechanically verify
    line numbers, symbols, file paths, field names, and counts against the
    actual codebase BEFORE adversary spawn. Catches factual drift cheaply
    so adversary rounds focus on architecture, not "you said line X but
    it's actually Y"."""
    assert "### 7.3a. Pre-flight fact-check" in body
    # CLI-agnostic — no specific model brand names.
    lowered = body.lower()
    for brand in ("sonnet", "haiku", "opus", "gpt-", "gemini-flash"):
        assert brand not in lowered, f"step 7.3a must be CLI-agnostic; found {brand!r}"
    assert "mid-tier subagent" in body
    # The step must enumerate the categories to verify.
    for keyword in (
        "line number",
        "symbol",
        "file path",
        "field",
        "count",
        "regex",
    ):
        assert keyword in lowered, f"step 7.3a must list verification category: {keyword!r}"


def test_step_7_3a_iterates_until_clean(body: str) -> None:
    """Pre-flight loops on drift (apply fixes, re-run) until the subagent
    reports no drift; only then proceed to anchor + spawn."""
    assert "Both `interactive` and `delegated` modes run this step every round" in body
    # Phrasing wraps across lines; check both fragments present.
    assert "Do NOT" in body
    assert "modify the source code being verified" in body
    assert "Re-run Step 7.3a" in body


def test_step_7_3a_revision_branches_route_through_factcheck(body: str) -> None:
    """When a round rejects and the planner revises, the next round must
    route through 7.3a (fact-check) before 7.4 (anchor + spawn). Skipping
    straight to 7.4 reintroduces the drift problem the pre-flight prevents."""
    assert "loop back to **Step 7.3a**" in body


def test_step_7_3a_dispatch_failure_falls_through(body: str) -> None:
    """Hosts that don't expose subagent dispatch must still run the loop —
    the pre-flight failure mode falls through to the adversary round, not
    aborts."""
    assert "fails to dispatch" in body
    assert "proceed directly to Step 7.4" in body
    assert "non-terminal review rejections" in body
