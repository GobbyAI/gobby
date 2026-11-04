"""Bundled agent YAML files must parse as valid agent definitions.

The only test that reads the bundled agent directory. It runs the same parse
step as sync_bundled_agents: safe-load, default the name to the file stem, then
validate through AgentDefinitionBody.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from gobby.agents.sync import get_bundled_agents_path
from gobby.cli.agents_steps import check_agent
from gobby.skills.sync import get_bundled_skills_path
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.definitions import (
    AgentDefinitionBody,
    RuleDefinitionBody,
    split_rule_definition_data,
)
from gobby.workflows.pipeline_models import PipelineDefinition
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.sync_pipelines import get_bundled_pipelines_path
from gobby.workflows.sync_rules import get_bundled_rules_path

pytestmark = pytest.mark.unit

BUNDLED_AGENT_YAMLS = sorted(get_bundled_agents_path().rglob("*.yaml"))


@pytest.mark.parametrize(
    "path", sorted(get_bundled_pipelines_path().glob("*.yaml")), ids=lambda path: path.stem
)
def test_discovered_pipeline_yaml_parses(path: Path) -> None:
    pipeline = PipelineDefinition.model_validate(yaml.safe_load(path.read_text()))
    assert pipeline.name


@pytest.mark.parametrize(
    "path",
    [
        path
        for path in sorted(get_bundled_rules_path().rglob("*.yaml"))
        if not {"deprecated", "_deprecated"}.intersection(path.parts)
    ],
    ids=lambda path: path.stem,
)
def test_discovered_rule_yaml_and_expressions_parse(path: Path) -> None:
    document = yaml.safe_load(path.read_text())
    for definition in document.get("rules", {}).values():
        definition = dict(definition)
        if "effect" in definition:
            effect = definition.pop("effect")
            definition.setdefault("effects", [effect])
        body, _ = split_rule_definition_data(definition)
        rule = RuleDefinitionBody.model_validate(body)
        assert rule.event
        for expression in [rule.when, *(effect.when for effect in rule.resolved_effects)]:
            if expression is not None:
                parsed = SafeExpressionEvaluator._parse_normalized_expr(
                    SafeExpressionEvaluator._normalize_expr(expression)
                )
                assert parsed.body is not None


def _validate_agent_definition(path: Path) -> AgentDefinitionBody:
    data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} is not a YAML mapping"
    name = data.get("name")
    data["name"] = name.strip() if isinstance(name, str) and name.strip() else path.stem
    return AgentDefinitionBody.model_validate(data)


@pytest.mark.parametrize("path", BUNDLED_AGENT_YAMLS, ids=lambda path: path.name)
def test_bundled_agent_yaml_is_valid_agent_definition(path: Path) -> None:
    definition = _validate_agent_definition(path)

    assert definition.name, f"{path.name} parsed without a name"


@pytest.mark.parametrize("path", BUNDLED_AGENT_YAMLS, ids=lambda path: path.name)
def test_agents_check_reports_valid_for_every_bundled_definition(
    path: Path, temp_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = _validate_agent_definition(path)
    AgentDefinitionManager(temp_db).upsert_from_sync(
        body.name,
        body.model_dump(mode="json"),
        body.step_workflow.model_dump(mode="json") if body.step_workflow else None,
    )
    monkeypatch.setattr("gobby.cli.agents_steps.require_cli_database", lambda: temp_db)
    monkeypatch.setattr("gobby.cli.agents_steps.get_project_context", lambda **kwargs: None)
    result = CliRunner().invoke(check_agent, [body.name])
    assert result.exit_code == 0, result.output
    assert "VALID" in result.output, result.output


def test_synthetic_definition_with_invalid_field_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "invalid-definition.yaml"
    path.write_text("name: invalid-definition\nworkflows: {}\nenabled: maybe-not\n")

    with pytest.raises(ValidationError) as excinfo:
        _validate_agent_definition(path)

    error_locations = [error["loc"] for error in excinfo.value.errors()]
    assert ("enabled",) in error_locations


def _load(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


BUNDLED_DEFINITIONS = {
    str(data.get("name") or path.stem): data
    for path in BUNDLED_AGENT_YAMLS
    if (data := _load(path))
}
BUNDLED_PIPELINES = [_load(path) for path in sorted(get_bundled_pipelines_path().glob("*.yaml"))]
TASK_RUNBOOKS = [pipeline for pipeline in BUNDLED_PIPELINES if pipeline.get("claim_flow")]


def _named_agents(node: object) -> set[str]:
    """Every literal ``agent`` a pipeline names, in seat catalogues and spawn steps."""
    found: set[str] = set()
    if isinstance(node, dict):
        if isinstance(node.get("agent"), str):
            found.add(node["agent"])
        for value in node.values():
            found |= _named_agents(value)
    elif isinstance(node, list):
        for item in node:
            found |= _named_agents(item)
    return found


def _team_seats() -> set[str]:
    """Seats the claim-flow runbooks launch, plus every seat those seats may spawn."""
    pending: set[str] = set()
    for pipeline in TASK_RUNBOOKS:
        pending |= _named_agents(pipeline)
    seats: set[str] = set()
    while pending:
        name = pending.pop()
        if name not in seats:
            seats.add(name)
            pending.update(BUNDLED_DEFINITIONS[name].get("spawnable_agents") or [])
    return seats


PLAN_REVIEW_SEATS = {
    name
    for pipeline in BUNDLED_PIPELINES
    for name in _named_agents(pipeline)
    if "track_plan_consensus" in (BUNDLED_DEFINITIONS[name].get("capabilities") or [])
}
CRITERIA_SEATS = sorted(_team_seats() | PLAN_REVIEW_SEATS)


def _prompts(name: str) -> dict[str, str]:
    prompts = BUNDLED_DEFINITIONS[name].get("prompts") or {}
    assert prompts, f"{name} has no prompts"
    return {surface: " ".join(str(text).split()) for surface, text in prompts.items()}


def _is_reviewer(name: str) -> bool:
    writes = BUNDLED_DEFINITIONS[name].get("task_attestation_writes") or []
    return "independent-review" in writes or name in PLAN_REVIEW_SEATS


def test_criteria_seat_set_is_derived_from_runbook_yaml() -> None:
    assert TASK_RUNBOOKS, "no bundled runbook declares claim_flow"
    assert PLAN_REVIEW_SEATS, "no runbook launches a plan-consensus seat"
    assert any(_is_reviewer(name) for name in _team_seats())


@pytest.mark.parametrize("name", CRITERIA_SEATS)
def test_runbook_seat_reads_and_applies_every_validation_criterion(name: str) -> None:
    for surface, text in _prompts(name).items():
        assert "get_task(brief=false)" in text, f"{name}.{surface}"
        assert "every validation criterion" in text, f"{name}.{surface}"
        if _is_reviewer(name):
            assert "cite each criterion" in text, f"{name}.{surface}"
            assert "an unmet criterion blocks" in text, f"{name}.{surface}"


def _review_stage_agents() -> set[str]:
    """Agents seated at each runbook's rejectable review stage."""
    agents: set[str] = set()
    for pipeline in TASK_RUNBOOKS:
        steps = {step["id"]: step for step in pipeline.get("steps") or []}
        for stage in pipeline["claim_flow"].values():
            if stage.get("on_rejected"):
                agents |= _named_agents(steps[stage["seat"]])
    return agents


EPIC_REVIEW_REFERENCE = "gobby:references/review/epic.md"
EPIC_REVIEW_DUTIES = (
    "## Holistic epic review",
    "get_task(brief=false)",
    "every attestation the selected policy requires",
    "made by the authorized seat for the exact candidate",
    "against the integrated epic branch",
    "the epic's own validation criteria and its parent plan",
    "gaps between children, integration seams",
    "A missing or invalid attestation or an unmet criterion blocks the epic.",
)


def test_review_stage_seat_loads_the_holistic_epic_review_for_epic_targets() -> None:
    agents = _review_stage_agents()
    assert agents
    for name in agents:
        for surface, text in _prompts(name).items():
            assert EPIC_REVIEW_REFERENCE in text, f"{name}.{surface}"

    raw = (get_bundled_skills_path() / "gobby" / "references" / "review" / "epic.md").read_text()
    reference = " ".join(raw.split())
    for duty in EPIC_REVIEW_DUTIES:
        assert duty in reference, duty
    assert re.search(r"^_Version: \d+\.\d+\.\d+_$", raw, re.MULTILINE)
