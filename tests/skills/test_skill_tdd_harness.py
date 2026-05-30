"""Behavioral scenario tests for bundled skills."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.skills.scenario_runner import run_recorded_skill_scenario

pytestmark = [pytest.mark.unit, pytest.mark.skill_tdd]

SCENARIOS = Path(__file__).resolve().parent / "scenarios"


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
    """Verify the loaded build coordinator scenario replaces manual waits with build fixes."""
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
        "monitor_dispatch",
        "inspect_coordination_epic_bugs",
        "fix_actionable_coordination_bug",
        "monitor_agents",
        "check_context_health",
        "compact_self_or_wait_for_agent",
        "verify_build_bugs_closed",
        "close_target",
        "close_coordination_epic",
        "respond",
    )
    assert result.has_behavioral_delta


def test_coderabbit_verifies_findings_before_fixing() -> None:
    """Verify the loaded CodeRabbit scenario inspects findings before fixing and cleans up reports."""
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


def test_code_index_uses_gcode_navigation_before_line_readers() -> None:
    """Verify loaded code-index behavior retrieves symbols before narrow line context."""
    result = run_recorded_skill_scenario(SCENARIOS / "code-index/gcode-before-line-readers.yaml")

    assert result.baseline.action_names == (
        "gcode_search",
        "broad_sed_read",
        "broad_file_read",
        "respond",
    )
    assert result.loaded.action_names == (
        "gcode_search",
        "gcode_outline",
        "gcode_symbol",
        "narrow_sed_context",
        "respond",
    )

    assert result.loaded.actions[2]["command"] == "gcode symbol <symbol-id-from-outline>"
    assert (
        result.loaded.actions[3]["command"]
        == "sed -n '<1-3 adjacent lines>' src/gobby/skills/parser.py"
    )
    assert result.has_behavioral_delta
