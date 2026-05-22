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


def test_writing_skills_requires_scenario_before_skill_body() -> None:
    result = run_recorded_skill_scenario(SCENARIOS / "writing-skills/create-discipline-skill.yaml")

    assert result.baseline.action_names == ("write_skill", "respond")
    assert result.loaded.action_names == (
        "add_pressure_scenario",
        "write_skill",
        "run_skill_tdd",
        "respond",
    )
    assert result.has_behavioral_delta


def test_build_coordinator_turns_manual_coordination_into_build_fixes() -> None:
    result = run_recorded_skill_scenario(
        SCENARIOS / "build-coordinator/unattended-build-coordination.yaml"
    )

    assert result.baseline.action_names == (
        "run_build",
        "wait_for_agent",
        "close_target",
        "respond",
    )
    assert result.loaded.action_names == (
        "create_coordination_epic",
        "inspect_dependency_tree",
        "normalize_leaf_stages",
        "launch_build",
        "monitor_agents",
        "file_build_bug",
        "fix_blocking_build_bug",
        "fix_non_blocking_build_bug",
        "verify_build_bugs_closed",
        "close_target",
        "close_coordination_epic",
        "respond",
    )
    assert result.has_behavioral_delta


def test_coderabbit_verifies_findings_before_fixing() -> None:
    result = run_recorded_skill_scenario(SCENARIOS / "coderabbit/verify-before-fixing.yaml")

    assert result.baseline.action_names == ("apply_finding", "leave_report", "respond")
    assert result.loaded.action_names == (
        "inspect_current_code",
        "document_no_fix",
        "apply_valid_finding",
        "delete_processed_report",
        "run_validation",
        "commit_and_close_task",
        "respond",
    )
    assert result.has_behavioral_delta
