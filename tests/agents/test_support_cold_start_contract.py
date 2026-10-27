"""Cold start contract (#25220): the Support seat set, launch defaults, consolidator roots and patrol."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.sync_pipelines import get_bundled_pipelines_path

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
POLICY = REPO / ".gobby/workflows/rules/project-operating-policy.yaml"
AGENTS = REPO / "src/gobby/install/shared/workflows/agents"
SUPPORT_SEATS = (
    "triage",
    "archivist",
    "security-reviewer",
    "release-manager",
    "recovery-steward",
    "consolidator",
)
CLAUDE_ONLY_SCHEDULERS = ("CronList", "CronCreate", "CronDelete")


def _policy_value() -> dict[str, Any]:
    effect = yaml.safe_load(POLICY.read_text())["rules"]["project-operating-policy"]["effects"][0]
    assert effect["variable"] == "project_policy"
    value: dict[str, Any] = effect["value"]
    return value


def _support() -> PipelineDefinition:
    path = get_bundled_pipelines_path() / "support.yaml"
    return PipelineDefinition.model_validate(yaml.safe_load(path.read_text()))


def _seat_key(seat: str) -> str:
    return seat.replace("-", "_")


def test_support_runbook_launches_exactly_the_six_cold_start_seats() -> None:
    definition = _support()
    guard = definition.steps[0].mcp
    assert guard is not None and guard.arguments is not None
    assert {entry["name"] for entry in guard.arguments["catalogue"]} == set(SUPPORT_SEATS)
    assert set(definition.inputs["seats"]["default"].split(",")) == set(SUPPORT_SEATS)


@pytest.mark.parametrize("seat", SUPPORT_SEATS)
def test_each_support_seat_is_launched_with_its_own_provider_model_and_effort(seat: str) -> None:
    definition = _support()
    key = _seat_key(seat)
    steps = [step for step in definition.steps if step.id == seat]
    assert steps, f"the Support runbook has no launch step for {seat}"
    assert steps[0].mcp is not None and steps[0].mcp.arguments is not None
    for suffix in ("provider", "model", "reasoning_effort"):
        assert f"{key}_{suffix}" in definition.inputs
        assert steps[0].mcp.arguments[suffix] == f"${{{{ inputs.{key}_{suffix} }}}}"


def test_project_policy_gives_every_support_seat_explicit_launch_defaults() -> None:
    support = _policy_value()["launch_defaults"]["support"]
    for seat in SUPPORT_SEATS:
        for suffix in ("provider", "model", "reasoning_effort"):
            assert support.get(f"{_seat_key(seat)}_{suffix}"), f"{seat} has no {suffix} default"


def test_security_reviewer_support_default_is_its_role_default() -> None:
    value = _policy_value()
    support = value["launch_defaults"]["support"]
    role = value["role_launch_defaults"]["security-reviewer"]
    assert (
        support.get("security_reviewer_provider"),
        support.get("security_reviewer_model"),
        support.get("security_reviewer_reasoning_effort"),
    ) == (role["provider"], role["model"], role["reasoning_effort"])


def test_recovery_steward_support_default_is_codex_gpt_6_1_luna_at_medium() -> None:
    support = _policy_value()["launch_defaults"]["support"]
    assert (
        support["recovery_steward_provider"],
        support["recovery_steward_model"],
        support["recovery_steward_reasoning_effort"],
    ) == ("codex", "gpt-6.1-luna", "medium")


def test_consolidator_first_job_names_only_current_roots() -> None:
    value = _policy_value()
    roots = {entry["epic"] for entry in value["roots"]}
    first_job = value["role_guidance"]["consolidator"].split("Exclude", 1)[0]
    named = set(re.findall(r"#\d+", first_job))
    assert named, "the consolidator's first job names no backlog root"
    assert named <= roots, f"first job names non-root refs {sorted(named - roots)}"


def test_recovery_steward_patrol_names_no_claude_only_scheduler() -> None:
    text = (AGENTS / "recovery-steward.yaml").read_text()
    assert [name for name in CLAUDE_ONLY_SCHEDULERS if name in text] == []


def test_recovery_steward_patrol_is_rearmed_by_a_ten_minute_wait_on_triage() -> None:
    """Pins the proposed mechanism: wait_for_coordination on Triage with reply=true, timeout=600."""
    text = (AGENTS / "recovery-steward.yaml").read_text()
    calls = [match.group(1) for match in re.finditer(r"wait_for_coordination\(([^)]*)\)", text)]
    assert any(
        "reply=true" in call and "timeout=600" in call and "triage" in call.lower()
        for call in calls
    )


def test_consolidator_definition_has_no_lane_language() -> None:
    text = (AGENTS / "consolidator.yaml").read_text()
    assert re.search(r"\blanes?\b", text, re.IGNORECASE) is None
