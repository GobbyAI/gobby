"""The bundled definitions and procedures carry the claim rules and the monitoring loop.

The tests read the bundled YAML and procedure references directly, so they pin the authority to the
definitions, never to a name in code. v5 (#25454) removed claim hand-offs between seats: the author
keeps the claim, reviewers observe without taking it, and messages never move a claim.
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
PROCEDURES = ROOT / "skills" / "gobby" / "references" / "agents" / "procedures"


def _definition(name: str) -> dict[str, Any]:
    loaded = yaml.safe_load((AGENTS / f"{name}.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.parametrize("name", ["dev-manager", "orchestrator"])
def test_monitoring_loop_waits_with_a_timeout_then_reads_team_progress(name: str) -> None:
    prompt = _definition(name)["prompts"]["agent"]

    assert "wait_for_coordination" in prompt
    assert "team_progress" in prompt
    assert "timeout" in prompt


def test_orchestrator_monitors_team_owners_by_status_not_reply() -> None:
    """DevTeam seats cannot reply to the Orchestrator, so its owner wait is a status wait."""
    prompt = " ".join(_definition("orchestrator")["prompts"]["agent"].split())

    assert "wait_for_coordination(owner_session=<owner ref>, reply=true" not in prompt
    assert 'statuses=["awaiting_input", "awaiting_approval", "awaiting_handoff"' in prompt
    assert "team_progress" in prompt


@pytest.mark.parametrize("name", ["dev-manager", "orchestrator"])
def test_manager_procedures_read_team_progress(name: str) -> None:
    procedure = (PROCEDURES / f"{name}.md").read_text(encoding="utf-8")

    assert "team_progress" in procedure


@pytest.mark.parametrize(
    ("name", "phrase"),
    [
        ("code-reviewer", "automatic escalation or claim hops"),
        ("docs-reviewer", "trigger no automatic claim"),
        ("research-reviewer", "trigger no automatic claim"),
    ],
)
def test_reviewer_procedures_name_no_automatic_claim_hop(name: str, phrase: str) -> None:
    procedure = (PROCEDURES / f"{name}.md").read_text(encoding="utf-8")

    assert phrase in procedure


def test_dev_manager_hands_claims_off_only_by_explicit_call() -> None:
    prompt = _definition("dev-manager")["prompts"]["agent"]
    procedure = (PROCEDURES / "dev-manager.md").read_text(encoding="utf-8")

    assert "gobby-tasks:hand_off_task_claim" in prompt
    assert "Messages never transfer claims" in prompt
    assert "move a claim by itself" in procedure


def test_manager_guidance_keeps_the_claim_with_its_author() -> None:
    prompt = _definition("dev-manager")["prompts"]["agent"]
    procedure = (PROCEDURES / "dev-manager.md").read_text(encoding="utf-8")

    assert "Authors claim their tasks" in prompt
    assert "without claiming implementation" in procedure


def test_test_writer_gate_reaches_default_seats() -> None:
    definition = _definition("test-writer")
    # The bundled edit gate reaches every default-tagged seat, and the Test Writer selects that tag.
    gate = yaml.safe_load(
        (RULES / "task-enforcement" / "require-task-before-edit.yaml").read_text(encoding="utf-8")
    )
    assert "default" in gate["tags"]
    assert "tag:default" in definition["workflows"]["rule_selectors"]["include"]
