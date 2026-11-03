"""The bundled Dev Team Lead: its write grant, schedule branches and team-tool routes."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

INSTALL = Path(__file__).parents[2] / "src/gobby/install/shared"
LEAD_YAML = INSTALL / "workflows/agents/dev-team-lead.yaml"
PROCEDURE = INSTALL / "skills/gobby/references/agents/procedures/dev-team-lead.md"


def _yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def _schedule_message() -> str:
    lead = _yaml(LEAD_YAML)
    schedule = next(step for step in lead["step_workflow"]["steps"] if step["name"] == "schedule")
    return str(schedule["status_message"])


def test_lead_writes_come_from_its_workspace_scope_not_a_blanket_block() -> None:
    lead = _yaml(LEAD_YAML)
    blocked = set(lead["blocked_tools"])

    assert {"Write", "write_file"}.isdisjoint(blocked)
    assert "Edit" in blocked
    assert "name:team-lead-write-scope" in lead["workflows"]["rule_selectors"]["include"]
    scope = _yaml(INSTALL / "workflows/rules/roles/seat-write-scope.yaml")
    assert scope["rules"]["team-lead-write-scope"]["event"] == "before_tool"


def test_schedule_asks_only_epic_teams_for_epic_assignment() -> None:
    paragraphs = _schedule_message().split("\n\n")

    single = [p for p in paragraphs if p.lstrip().startswith("Single-task team")]
    assert len(single) == 1, paragraphs
    assert "phase=wait" in single[0]
    assert "no epic_assignment" in single[0]
    persist = next(p for p in paragraphs if "Persist epic_assignment" in p)
    assert persist.lstrip().startswith("Epic team")


def test_lead_reads_team_state_through_gobby_tasks_routes() -> None:
    bare = re.compile(r"(?<![\w:-])(team_progress|get_team)\b")
    lead_text = LEAD_YAML.read_text()
    procedure = PROCEDURE.read_text()

    assert not bare.search(lead_text), bare.findall(lead_text)
    assert not bare.search(procedure), bare.findall(procedure)
    assert "gobby-tasks:team_progress" in lead_text
    assert "gobby-tasks:team_progress" in procedure
    assert "gobby-tasks:get_team" in procedure
