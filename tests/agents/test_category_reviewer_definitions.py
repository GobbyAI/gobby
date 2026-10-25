"""Docs and research reviewers, the reviewer routing table, and their runbook binding (7.3)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from gobby.agents.sync import get_bundled_agents_path, sync_bundled_agents
from gobby.storage.definitions import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import AgentDefinitionBody, PipelineDefinition
from tests.agents._yaml_helpers import flat

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "src/gobby/install/shared"
AGENTS = get_bundled_agents_path()
PROCEDURES = SHARED / "skills/gobby/references/agents/procedures"
ROLE_RULES = SHARED / "workflows/rules/roles"
DEVTEAM = SHARED / "workflows/pipelines/devteam.yaml"
NEW_REVIEWERS = ("docs-reviewer", "research-reviewer")
EDIT_TOOLS = {"Edit", "Write", "NotebookEdit"}
ROUTING_TABLE = """
| Task category | Reviewer |
| --- | --- |
| code, config, refactor, test | `code-reviewer` |
| docs, subject is code behavior | `code-reviewer` |
| docs, any other subject | `docs-reviewer` |
| research | `research-reviewer` |
"""
# The docs-about-code rule: a docs task goes to code-reviewer when checking it reads source.
DOCS_ABOUT_CODE = "requires reading source"
EPIC_TIP_PRECEDENCE = re.compile(
    r"`code-reviewer`, then `docs-reviewer`, then `research-reviewer`", re.IGNORECASE
)


def _agent(name: str) -> AgentDefinitionBody:
    path = AGENTS / f"{name}.yaml"
    assert path.is_file(), f"no bundled {name} definition"
    return AgentDefinitionBody.model_validate(yaml.safe_load(path.read_text()))


def _table_rows(text: str) -> list[str]:
    """Markdown table rows of the reviewer routing table, whitespace-normalized."""
    rows = [" ".join(line.split()) for line in text.splitlines() if line.lstrip().startswith("|")]
    start = next(
        (index for index, row in enumerate(rows) if row == "| Task category | Reviewer |"), None
    )
    assert start is not None, "no reviewer routing table"
    table: list[str] = []
    for row in rows[start:]:
        if table and not row.startswith("|"):
            break
        table.append(row)
        if len(table) == 6:
            break
    return table


def _seat_catalogues(path: Path) -> list[list[str]]:
    """Each rule's inline `_agent_type in [...]` seat catalogue in a role rule file."""
    rules = yaml.safe_load(path.read_text())["rules"]
    catalogues = []
    for rule in rules.values():
        match = re.search(r"_agent_type'\) in \[([^\]]*)\]", rule.get("when") or "")
        if match:
            catalogues.append(re.findall(r"'([^']+)'", match[1]))
    assert catalogues, path.name
    return catalogues


@pytest.mark.parametrize("name", NEW_REVIEWERS)
def test_reviewer_definitions_are_read_only_review_writers(temp_db: HubDatabase, name: str) -> None:
    body = _agent(name)
    assert body.name == name
    assert body.task_step_writes == ["review"]
    assert body.provider == "inherit"
    assert EDIT_TOOLS <= set(body.blocked_tools), body.blocked_tools
    assert body.supports_surface("spawn")

    procedure = f"gobby:references/agents/procedures/{name}.md"
    assert body.workflows.variables.get("_seat_procedure") == procedure
    assert body.step_workflow is not None
    assert procedure in body.step_workflow.variables["required_skills"]
    text = flat((PROCEDURES / f"{name}.md").read_text())
    assert "record_review_step" in text
    assert re.search(r"\bnever edit", text, re.IGNORECASE), name
    assert re.search(r"\bonly review", text, re.IGNORECASE), name
    if name == "research-reviewer":
        assert "gcode evidence" in text
        assert re.search(r"question asked is the question answered", text, re.IGNORECASE)

    sync_bundled_agents(temp_db)
    installed = AgentDefinitionManager(temp_db).get_by_name(name)
    assert installed is not None and installed.enabled
    assert installed.definition_json["task_step_writes"] == ["review"]


def test_routing_table_matches_procedures() -> None:
    expected = _table_rows(ROUTING_TABLE)
    for procedure in ("orchestrator.md", "dev-manager.md"):
        text = (PROCEDURES / procedure).read_text()
        assert _table_rows(text) == expected, procedure
        assert DOCS_ABOUT_CODE in flat(text), procedure
        assert EPIC_TIP_PRECEDENCE.search(flat(text)), procedure


def test_runbook_and_seat_rules_cover_new_reviewers() -> None:
    raw = yaml.safe_load(DEVTEAM.read_text())
    assert "reviewer_agent" in raw["inputs"], sorted(raw["inputs"])
    assert raw["inputs"]["reviewer_agent"].get("required") is True, raw["inputs"]
    devteam = PipelineDefinition.model_validate(raw)

    admit = next(step for step in devteam.steps if step.id == "admit")
    assert admit.mcp is not None and admit.mcp.arguments is not None
    assert admit.mcp.arguments["reviewer_agent"] == "${{ inputs.reviewer_agent }}"
    agents = [
        step.mcp.arguments.get("agent")
        for step in devteam.steps
        if step.mcp is not None and step.mcp.tool == "spawn_agent" and step.mcp.arguments
    ]
    assert agents.count("${{ inputs.reviewer_agent }}") == 1, agents
    assert "code-reviewer" not in agents, agents

    for path in sorted(ROLE_RULES.glob("*.yaml")):
        text = path.read_text()
        if "'code-reviewer'" not in text or "_agent_type') in [" not in text:
            continue
        for catalogue in _seat_catalogues(path):
            if "code-reviewer" in catalogue:
                assert set(NEW_REVIEWERS) <= set(catalogue), path.name
    for name in ("inject-seat-common", "seat-spawn-policy", "seat-no-ask-user-question"):
        for catalogue in _seat_catalogues(ROLE_RULES / f"{name}.yaml"):
            assert set(NEW_REVIEWERS) <= set(catalogue), name
