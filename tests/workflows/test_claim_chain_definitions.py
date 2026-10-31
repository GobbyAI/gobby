"""The bundled definitions write the claim chain: the hops, the fixer gate, the monitoring loop.

The engine hop only runs where a definition declares it, so these tests read the bundled YAML and
procedure references directly. They pin the authority to the definitions, never to a name in code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2] / "src" / "gobby" / "install" / "shared"
AGENTS = ROOT / "workflows" / "agents"
RULES = ROOT / "workflows" / "rules"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}
PROCEDURES = ROOT / "skills" / "gobby" / "references" / "agents" / "procedures"


def _definition(name: str) -> dict[str, Any]:
    loaded = yaml.safe_load((AGENTS / f"{name}.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _step(name: str, step: str) -> dict[str, Any]:
    steps: list[dict[str, Any]] = _definition(name)["step_workflow"]["steps"]
    (found,) = [item for item in steps if item["name"] == step]
    return found


def _hops(name: str, step: str) -> set[tuple[str, str]]:
    """The (tool, hop) pairs a step's successful calls hand the claim along."""
    return {
        (handler["tool"], handler["hop"])
        for handler in _step(name, step).get("on_mcp_success", [])
        if handler.get("action") == "hand_off_claim"
    }


def test_developer_candidate_hands_the_claim_to_the_reviewer() -> None:
    assert ("send_message", "review") in _hops("developer", "submit")


def test_test_writer_claims_before_writing_and_hands_its_candidate_to_the_reviewer() -> None:
    definition = _definition("test-writer")

    assert "step_workflow" in definition
    claim_steps = [
        item["name"]
        for item in definition["step_workflow"]["steps"]
        if "gobby-tasks:claim_task" in item.get("allowed_mcp_tools", [])
    ]
    assert claim_steps
    assert any(
        ("send_message", "review") in _hops("test-writer", item["name"])
        for item in definition["step_workflow"]["steps"]
    )


@pytest.mark.parametrize("name", ["code-reviewer", "docs-reviewer"])
def test_reviewer_verdict_hands_the_claim_back_on_bounce_and_onward_on_land(name: str) -> None:
    hops = _hops(name, "verdict")

    assert ("send_message", "maker") in hops
    assert ("send_message", "next_open") in hops


def test_reviewer_await_takes_the_claim_only_at_the_second_rejection() -> None:
    claims = [
        handler
        for handler in _step("code-reviewer", "await").get("on_mcp_success", [])
        if handler.get("tool") == "claim_task"
    ]

    assert len(claims) == 1
    assert "review_fail_count" in claims[0]["when"]


@pytest.mark.parametrize("name", ["dev-manager", "orchestrator"])
def test_monitoring_loop_waits_with_a_timeout_then_reads_team_progress(name: str) -> None:
    prompt = _definition(name)["prompts"]["agent"]

    assert "wait_for_coordination" in prompt
    assert "team_progress" in prompt
    assert "timeout" in prompt


@pytest.mark.parametrize(
    "name",
    ["dev-manager", "orchestrator", "code-reviewer", "docs-reviewer", "research-reviewer"],
)
def test_procedure_reads_team_progress_and_names_the_automatic_hop(name: str) -> None:
    procedure = (PROCEDURES / f"{name}.md").read_text(encoding="utf-8")

    assert "team_progress" in procedure
    assert "automatic" in procedure.lower()


def test_dev_manager_procedure_keeps_the_proof_request_hand_off() -> None:
    procedure = (PROCEDURES / "dev-manager.md").read_text(encoding="utf-8")

    assert "hand_off_task_claim" in procedure
    assert "PROOF_REQUEST" in procedure


def test_test_writer_edits_only_after_its_claim() -> None:
    definition = _definition("test-writer")
    # The bundled edit gate reaches every default-tagged seat, and the Test Writer selects that tag.
    gate = yaml.safe_load(
        (RULES / "task-enforcement" / "require-task-before-edit.yaml").read_text(encoding="utf-8")
    )
    assert "default" in gate["tags"]
    assert "tag:default" in definition["workflows"]["rule_selectors"]["include"]
    # The write step, the only one that edits, is reached through the claim. Earlier steps
    # expose no edit tool.
    steps = {item["name"]: item for item in definition["step_workflow"]["steps"]}
    assert steps["write"]["allowed_tools"] == "all"
    for earlier in ("load_skills", "claim"):
        assert not EDIT_TOOLS & set(steps[earlier].get("allowed_tools", []))
    assert any(
        transition["to"] == "write" and "task_claimed" in transition.get("when", "")
        for transition in steps["claim"]["transitions"]
    )
