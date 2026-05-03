"""Behavioral scenario tests for bundled skills."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.skills.scenario_runner import run_recorded_skill_scenario

pytestmark = [pytest.mark.unit, pytest.mark.skill_tdd]

SCENARIOS = Path(__file__).resolve().parent / "scenarios"


def test_verification_before_completion_changes_completion_behavior() -> None:
    result = run_recorded_skill_scenario(
        SCENARIOS / "verification-before-completion/completion-claim.yaml"
    )

    assert result.baseline.action_names == ("respond",)
    assert result.loaded.action_names == ("run_verification", "respond")
    assert result.has_behavioral_delta
