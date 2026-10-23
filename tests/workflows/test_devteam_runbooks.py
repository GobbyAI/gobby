"""Acceptance tests for the Support and DevTeam runbooks and their seat definitions.

Admin is the operator's Assistant and Orchestrator panes, which `/gobby admin` and
`/gobby assistant` set up in one tab; no runbook launches them. The Support runbook
launches the standing seats into their own tab. A DevTeam is the four seats of one task
tree. The Log and Metrics Monitors stay bundled, and no runbook launches them.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path, sync_bundled_agents
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.definitions.agents import SYNC_ORPHAN_TAG
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import AgentDefinitionBody, PipelineDefinition
from tests.agents._yaml_helpers import flat

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "src/gobby/install/shared"
PIPELINES = SHARED / "workflows/pipelines"
AGENTS = get_bundled_agents_path()
SKILLS = SHARED / "skills/gobby"
PROCEDURES = SKILLS / "references/agents/procedures"
REPORTER_SOURCES = ROOT / "src/gobby/agents/reporter_sources.py"

SUPPORT_SEATS = ["release-manager", "triage", "archivist", "recovery-steward", "consolidator"]
PERSONA_PANES = ("assistant", "orchestrator")
MONITORS = ("log-monitor", "metrics-monitor")
RUNBOOKS_GUIDE = ROOT / "docs/guides/runbooks.md"
DEVTEAM_SEATS = ["dev-manager", "test-writer", "developer", "code-reviewer"]
RETIRED_AGENTS = ("lane-manager", "inbox-manager", "task-close-reviewer")
RETIRED_RUNBOOKS = ("team-coordination", "team-lane")
# No source names these. The close reviewer's name stays where old runs are refused a resume.
RETIRED_NAMES = ("lane-manager", "inbox-manager", *RETIRED_RUNBOOKS)
RETAINED_AGENTS = (
    "log-monitor",
    "metrics-monitor",
    "recovery-steward",
    "expansion-qa",
    "researcher",
    "security-reviewer",
    "consolidator",
    "triage-agent",
    "plan-writer",
    "plan-enhancer",
    "plan-adversary",
)
STEP_WRITERS = {
    "test-writer": ["test"],
    "developer": ["implementation"],
    "code-reviewer": ["review"],
    "dev-manager": ["closing"],
}
REJECTION_2_LINE = (
    "EVENT=REJECTION_2 TASK=#<seq> TEAM=<team id> COMMIT=<40-hex candidate> "
    "REVIEWERS=<session ref>[,<session ref>...]"
)
# Retired relays: lane events, activation and landing receipts, and TDD proof results.
RETIRED_RELAYS = re.compile(
    r"LANE=|EVENT=PROOF_RESULT|RECEIPT|ACTIVATION=|Lane Manager|Inbox Manager"
)
HEAVY_RUN_GUIDANCE = re.compile(
    r"heavy[- ]?(?:run|slot|command)s?|CARGO_BUILD_JOBS|slot (?:grant|request)|"
    r"(?:grant|request)s? (?:a |the )?slot",
    re.IGNORECASE,
)


def _pipeline(name: str) -> PipelineDefinition:
    return PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / f"{name}.yaml").read_text())
    )


def _agent(name: str) -> AgentDefinitionBody:
    return AgentDefinitionBody.model_validate(yaml.safe_load((AGENTS / f"{name}.yaml").read_text()))


def _catalogue(definition: PipelineDefinition) -> list[dict[str, Any]]:
    guard = definition.steps[0].mcp
    assert guard is not None and guard.tool == "check_runbook_seats"
    assert guard.arguments is not None and guard.arguments["require_report_to"] is True
    catalogue = guard.arguments["catalogue"]
    assert isinstance(catalogue, list)
    return catalogue


def _spawns(definition: PipelineDefinition) -> dict[str, dict[str, Any]]:
    spawns: dict[str, dict[str, Any]] = {}
    for step in definition.steps:
        if step.mcp is not None and step.mcp.tool == "spawn_agent":
            assert step.mcp.arguments is not None, step.id
            spawns[step.id] = step.mcp.arguments
    return spawns


def _conditions(definition: PipelineDefinition) -> dict[str, str | None]:
    return {
        step.id: step.condition
        for step in definition.steps
        if step.mcp is not None and step.mcp.tool == "spawn_agent"
    }


def _guidance(name: str) -> str:
    """The definition's agent prompt plus its seat procedure, whitespace-collapsed."""
    body = _agent(name)
    assert body.prompts is not None and body.prompts.agent, name
    text = body.prompts.agent
    procedure = body.workflows.variables.get("_seat_procedure")
    if procedure:
        text += "\n" + (SKILLS / procedure.split(":", 1)[1]).read_text()
    return flat(text)


def test_runbook_seat_catalogues() -> None:
    runbooks = sorted(
        path.stem
        for path in PIPELINES.glob("*.yaml")
        if "runbook" in (yaml.safe_load(path.read_text()).get("tags") or [])
    )
    assert runbooks == ["devteam", "planning", "support", "team-planning"]
    # Admin panes are operator shells that the cold start sets up, never a runbook.
    assert not (PIPELINES / "admin.yaml").exists()
    for pane in PERSONA_PANES:
        assert _agent(pane).surfaces == ["persona"], pane

    support = _pipeline("support")
    assert support.resume_on_restart
    guard = support.steps[0].mcp
    assert guard is not None and guard.arguments is not None
    # A re-run skips seats that are already live, so it never duplicates one.
    assert guard.arguments["skip_live_seats"] is True
    assert [entry["name"] for entry in _catalogue(support)] == SUPPORT_SEATS
    assert [entry["agent"] for entry in _catalogue(support)] == SUPPORT_SEATS
    support_spawns = _spawns(support)
    assert list(support_spawns) == SUPPORT_SEATS
    for seat, arguments in support_spawns.items():
        assert arguments["agent"] == seat
        assert arguments["checkout_mode"] == "none"
        assert arguments["project_path"] == "${{ inputs.project_path }}"
        assert "task_id" not in arguments
        assert "Support" in str(arguments["placement"]), seat
    for seat, condition in _conditions(support).items():
        assert f"'{seat}' in steps.guard.output.launch" in (condition or ""), seat
    for name in runbooks:
        agents = {entry["agent"] for entry in _catalogue(_pipeline(name))}
        assert agents.isdisjoint(MONITORS), name

    devteam = _pipeline("devteam")
    assert [step.id for step in devteam.steps[:2]] == ["guard", "admit"]
    assert [entry["agent"] for entry in _catalogue(devteam)] == DEVTEAM_SEATS
    devteam_spawns = _spawns(devteam)
    assert list(devteam_spawns) == DEVTEAM_SEATS
    for seat, arguments in devteam_spawns.items():
        assert arguments["agent"] == seat
        assert arguments["checkout_mode"] == "none"
        assert arguments["project_path"] == "${{ steps.admit.output.project_path }}"
    assert [seat for seat, arguments in devteam_spawns.items() if "task_id" in arguments] == [
        "dev-manager"
    ]

    for seat in SUPPORT_SEATS + DEVTEAM_SEATS:
        body = _agent(seat)
        assert body.name == seat
        assert body.supports_surface("spawn"), seat


def test_agent_retirement_and_retention(temp_db: HubDatabase, tmp_path: Path) -> None:
    for name in RETIRED_AGENTS:
        assert not (AGENTS / f"{name}.yaml").exists(), name
        assert not (PROCEDURES / f"{name}.md").exists(), name
    for name in RETIRED_RUNBOOKS:
        assert not (PIPELINES / f"{name}.yaml").exists(), name
    retired = re.compile("|".join(RETIRED_NAMES))
    sources = [
        path
        for pattern in ("**/*.py", "**/*.yaml", "**/*.md")
        for path in (ROOT / "src/gobby").glob(pattern)
        if "deprecated" not in path.parts
    ]
    assert [str(path) for path in sources if retired.search(path.read_text())] == []

    # An upgrade from a release that shipped the old managers removes their installed rows.
    previous = tmp_path / "agents"
    shutil.copytree(AGENTS, previous, ignore=shutil.ignore_patterns("deprecated"))
    for name in RETIRED_AGENTS:
        (previous / f"{name}.yaml").write_text(
            f"name: {name}\ndescription: Retired seat\nprompts:\n  agent: Retired.\n"
        )
    with patch("gobby.agents.sync.get_bundled_agents_path", return_value=previous):
        assert sync_bundled_agents(temp_db)["errors"] == []
    result = sync_bundled_agents(temp_db)
    assert result["errors"] == []

    manager = AgentDefinitionManager(temp_db)
    for name in RETIRED_AGENTS:
        assert manager.get_by_name(name) is None, name
        swept = manager.get_by_name(name, include_deleted=True)
        assert swept is not None and SYNC_ORPHAN_TAG in (swept.tags or []), name
    for name in RETAINED_AGENTS + tuple(SUPPORT_SEATS) + tuple(DEVTEAM_SEATS):
        row = manager.get_by_name(name)
        assert row is not None and row.enabled, name
        body = AgentDefinitionBody.model_validate(row.definition_json)
        assert body.supports_surface("spawn"), name

    expansion = flat(_agent("expansion-qa").description)
    assert "runbook" in expansion and "dependenc" in expansion


def test_procedures_match_runtime_authority() -> None:
    for seat, writes in STEP_WRITERS.items():
        assert _agent(seat).task_step_writes == writes, seat
        assert f"record_{writes[0]}_step" in _guidance(seat), seat
    assert set(_agent("orchestrator").task_step_writes) == {
        "test",
        "implementation",
        "review",
        "closing",
    }

    manager = _agent("dev-manager")
    assert manager.workflows.variables.get("team_manager") is True
    assert "gobby:references/agents/gclient.md" in manager.workflows.variables["_seat_references"]
    dev_manager = _guidance("dev-manager")
    for phrase in (
        "hand_off_task_claim",
        REJECTION_2_LINE,
        "land_commit",
        "close_task",
        "summary",
        "lessons",
        "release window",
    ):
        assert phrase in dev_manager, phrase
    assert dev_manager.index("record_closing_step") < dev_manager.index("close_task")

    test_writer = _guidance("test-writer")
    assert "commit" in test_writer and "before implementation" in test_writer
    reviewer = _guidance("code-reviewer")
    for phrase in ("security", "run the tests", "test-first history", "exact SHA"):
        assert phrase in reviewer, phrase

    orchestrator = _guidance("orchestrator")
    for phrase in (
        "fresh_reviewer=true",
        "exclude_reviewers=",
        "launch_defaults.devteam",
        "escalate_task",
        "/gobby admin",
        "launch_defaults.support",
    ):
        assert phrase in orchestrator, phrase
    assert "/gobby assistant" in _guidance("assistant")

    triage = _agent("triage")
    assert {"session"} <= set(triage.send_message_targets)
    triage_text = _guidance("triage")
    for phrase in ("wait_for_coordination(timeout=300)", "wake=false", "EVENT=REJECTION_2"):
        assert phrase in triage_text, phrase

    release = _agent("release-manager")
    assert "global" in release.send_message_targets
    assert "tag:worker-safety" in release.workflows.rule_selectors.include
    release_text = _guidance("release-manager")
    for phrase in ("gobby release batch", "never renew", "Triage"):
        assert phrase in release_text, phrase

    for seat in ("log-monitor", "metrics-monitor", *DEVTEAM_SEATS, "release-manager"):
        text = _guidance(seat)
        assert "Triage" in text, seat
        assert RETIRED_RELAYS.findall(text) == [], seat


def test_runbooks_guide_opens_with_the_cold_start() -> None:
    guide = RUNBOOKS_GUIDE.read_text()
    opening = guide.split("\n## ", 2)[1]
    for command in ("/gobby admin", "$gobby admin", "/gobby assistant", "$gobby assistant"):
        assert command in opening, command
    assert "Support" in guide and "DevTeam" in guide


def test_no_heavy_run_slot_guidance() -> None:
    sources = [
        *(path for path in AGENTS.glob("*.yaml")),
        *PIPELINES.glob("*.yaml"),
        *PROCEDURES.glob("*.md"),
        REPORTER_SOURCES,
    ]
    found = {
        str(path.relative_to(ROOT)): matches
        for path in sources
        if (matches := HEAVY_RUN_GUIDANCE.findall(path.read_text()))
    }
    assert found == {}
