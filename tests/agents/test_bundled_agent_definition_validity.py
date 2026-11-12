"""Bundled agent YAML files must parse as valid agent definitions.

The only test that reads the bundled agent directory. It runs the same parse
step as sync_bundled_agents: safe-load, default the name to the file stem, then
validate through AgentDefinitionBody.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from gobby.agents.sync import get_bundled_agents_path
from gobby.cli.agents_steps import check_agent
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.clones import create_clones_registry
from gobby.mcp_proxy.tools.merge import create_merge_registry
from gobby.mcp_proxy.tools.worktrees._factory import create_worktrees_registry
from gobby.skills.sync import get_bundled_skills_path
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager, RuleDefinitionRow
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows.condition_helpers_git_tools import GIT_MUTATING_MCP_TOOLS
from gobby.workflows.definitions import (
    AgentDefinitionBody,
    RuleDefinitionBody,
    WorkflowStep,
    split_rule_definition_data,
)
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.engine.enforcement_checks import EnforcementCheckMixin
from gobby.workflows.pipeline_models import PipelineDefinition
from gobby.workflows.safe_evaluator import SafeExpressionEvaluator
from gobby.workflows.selectors import resolve_rules_for_agent
from gobby.workflows.step_instances import AgentStepInstance
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
    from gobby.workflows.rule_validation import validate_rule

    document = yaml.safe_load(path.read_text())
    for definition in document.get("rules", {}).values():
        definition = dict(definition)
        if "effect" in definition:
            effect = definition.pop("effect")
            definition.setdefault("effects", [effect])
        body, _ = split_rule_definition_data(definition)
        rule = RuleDefinitionBody.model_validate(body)
        report = validate_rule(body)
        assert report.valid, report
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


def _guard_tab_titles(node: object) -> list[str]:
    """Every literal ``tab_title`` a pipeline passes to the seat guard, at any depth."""
    found: list[str] = []
    if isinstance(node, dict):
        arguments = node.get("arguments")
        if node.get("tool") == "check_runbook_seats" and isinstance(arguments, dict):
            if isinstance(arguments.get("tab_title"), str):
                found.append(arguments["tab_title"])
        for value in node.values():
            found.extend(_guard_tab_titles(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_guard_tab_titles(item))
    return found


GUARD_TAB_TITLES = {
    str(pipeline.get("name")): _guard_tab_titles(pipeline) for pipeline in BUNDLED_PIPELINES
}
GUARDED_RUNBOOKS = sorted(name for name, titles in GUARD_TAB_TITLES.items() if titles)


def test_guarded_runbooks_are_discovered() -> None:
    assert GUARDED_RUNBOOKS


@pytest.mark.parametrize("name", GUARDED_RUNBOOKS)
def test_guard_tab_title_is_a_bare_runbook_name(name: str) -> None:
    """The guard appends ' - #<seq>' itself, so a runbook passes only its bare name."""
    for tab_title in GUARD_TAB_TITLES[name]:
        assert "#" not in tab_title, f"{name} passes a task ref in tab_title: {tab_title!r}"
        assert "inputs.task" not in tab_title, f"{name} passes the task in tab_title: {tab_title!r}"


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
                if "seat" in stage:
                    agents |= _named_agents(steps[stage["seat"]])
                else:
                    agents.add(stage["agent"])
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


# A seat told to create and claim its own task does so first; a skill-loading
# first step must not refuse that claim (#25580 items 9 and 31).
SELF_CLAIM_DIRECTIVE = re.compile(r"create_task\W.{0,120}claim=true")
SELF_CLAIMING_STEP_SEATS = sorted(
    name
    for name, data in BUNDLED_DEFINITIONS.items()
    if data.get("step_workflow")
    and any(
        SELF_CLAIM_DIRECTIVE.search(" ".join(str(text).split()))
        for text in (data.get("prompts") or {}).values()
    )
)


class _FirstStepHarness(EnforcementCheckMixin):
    """The engine's step check on a definition's first step, with no bound run."""

    def __init__(self, step: WorkflowStep, agent_name: str) -> None:
        self._step = step
        self._instance = MagicMock(spec=AgentStepInstance)
        self._instance.agent_name = agent_name
        self._instance.variables = {}
        self.instance_manager = MagicMock()

    def _get_step_for_session(
        self, session_id: str
    ) -> tuple[WorkflowStep | None, AgentStepInstance | None]:
        del session_id
        return self._step, self._instance

    def _active_agent_run(self, session_id: str) -> tuple[Any, Any] | None:
        del session_id
        return None

    def _audit_step_tool_call(self, *args: object, **kwargs: object) -> None:
        del args, kwargs


def test_self_claiming_step_seats_are_discovered() -> None:
    assert SELF_CLAIMING_STEP_SEATS


@pytest.mark.parametrize("name", SELF_CLAIMING_STEP_SEATS)
def test_first_step_admits_the_seats_own_task_claim(name: str) -> None:
    data = dict(BUNDLED_DEFINITIONS[name], name=name)
    workflow = AgentDefinitionBody.model_validate(data).step_workflow
    assert workflow is not None
    harness = _FirstStepHarness(workflow.steps[0], name)
    session_id = "11111111-1111-4111-8111-111111111111"
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=session_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "mcp__gobby__call_tool",
            "tool_input": {
                "server_name": "gobby-tasks",
                "tool_name": "create_task",
                "arguments": {"title": "Own task", "claim": True},
            },
        },
        # Stop after the step's own allow-list; handlers are not under test.
        metadata={"_mcp_proxy_duplicate_before_tool": True},
    )

    response = harness._check_step_tool_enforcement_locked(event, session_id, {})

    assert response is None, response.reason if response else None


SHELL_GIT_MUTATION_FIELD = "canonical_git_mutation"
READ_ONLY_MCP_CALL = ("gobby-worktrees", "get_worktree")
REVIEW_ATTESTATIONS = {"independent-review", "security-review"}
_SYNCED_AT = datetime(2026, 11, 3, tzinfo=UTC)


def _bundled_rule_rows() -> list[RuleDefinitionRow]:
    """Bundled rules as sync stores them: per-rule tags, else file tags, plus gobby."""
    rows: list[RuleDefinitionRow] = []
    for path in sorted(get_bundled_rules_path().rglob("*.yaml")):
        if {"deprecated", "_deprecated"}.intersection(path.parts):
            continue
        document = _load(path)
        for name, definition in (document.get("rules") or {}).items():
            tags = list(definition.get("tags", document.get("tags", [])))
            rows.append(
                RuleDefinitionRow(
                    id=name,
                    name=name,
                    enabled=True,
                    enabled_pinned=False,
                    priority=int(definition.get("priority", 100)),
                    definition_json=dict(definition),
                    source="installed",
                    tags=tags if "gobby" in tags else [*tags, "gobby"],
                    created_at=_SYNCED_AT,
                    updated_at=_SYNCED_AT,
                )
            )
    return rows


BUNDLED_RULE_ROWS = _bundled_rule_rows()
SHELL_GIT_BLOCKING_RULES = {
    row.name
    for row in BUNDLED_RULE_ROWS
    if SHELL_GIT_MUTATION_FIELD in str(row.definition_json.get("when", ""))
    and any(effect.get("type") == "block" for effect in row.definition_json.get("effects", []))
}
AGENT_BODIES = {body.name: body for body in map(_validate_agent_definition, BUNDLED_AGENT_YAMLS)}


def _blocks_shell_git_mutation(body: AgentDefinitionBody) -> bool:
    return bool(resolve_rules_for_agent(body, BUNDLED_RULE_ROWS) & SHELL_GIT_BLOCKING_RULES)


SHELL_GIT_BLOCKED_SEATS = sorted(
    name for name, body in AGENT_BODIES.items() if _blocks_shell_git_mutation(body)
)
TREE_REVIEW_SEATS = sorted(
    name
    for name, body in AGENT_BODIES.items()
    if body.checkout_mode == "inherit" and REVIEW_ATTESTATIONS & set(body.task_attestation_writes)
)


def test_branch_guard_invariants_cover_bundled_seats() -> None:
    assert SHELL_GIT_BLOCKING_RULES, "no bundled rule blocks shell git mutation"
    assert TREE_REVIEW_SEATS, "no bundled review seat runs in an inherited checkout"


@pytest.mark.parametrize("name", TREE_REVIEW_SEATS)
def test_review_seat_in_the_author_tree_cannot_mutate_git_from_the_shell(name: str) -> None:
    assert _blocks_shell_git_mutation(AGENT_BODIES[name]), name


def _registered_mcp_tools() -> set[tuple[str, str]]:
    registries = (
        create_worktrees_registry(MagicMock()),
        create_clones_registry(MagicMock(), None, None),
        create_merge_registry(MagicMock(), MagicMock()),
    )
    return {
        (registry.name, tool["name"]) for registry in registries for tool in registry.list_tools()
    }


def _seat_git_engine(db: HubDatabase, name: str) -> RuleEngine:
    """Install the shell-git blocking rules the seat selects, as sync stores them."""
    selected = resolve_rules_for_agent(AGENT_BODIES[name], BUNDLED_RULE_ROWS)
    manager = RuleDefinitionManager(db)
    for row in BUNDLED_RULE_ROWS:
        if row.name in selected & SHELL_GIT_BLOCKING_RULES:
            body = RuleDefinitionBody.model_validate(
                {key: row.definition_json[key] for key in ("event", "when", "effects")}
            )
            manager.create(
                name=row.name,
                tags=["default"],
                priority=row.priority,
                definition_json=body.model_dump(mode="json"),
            )
    return RuleEngine(db)


def _mcp_call(server: str, tool: str) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id="22222222-2222-4222-8222-222222222222",
        source=SessionSource.CLAUDE,
        timestamp=_SYNCED_AT,
        data={
            "tool_name": "mcp__gobby__call_tool",
            "mcp_server": server,
            "mcp_tool": tool,
            "tool_input": {"server_name": server, "tool_name": tool, "arguments": {}},
        },
    )


def test_declared_git_mutating_mcp_tools_are_registered() -> None:
    registered = _registered_mcp_tools()
    assert sorted(GIT_MUTATING_MCP_TOOLS - registered) == []
    assert READ_ONLY_MCP_CALL in registered


@pytest.mark.asyncio
@pytest.mark.parametrize("name", SHELL_GIT_BLOCKED_SEATS)
async def test_seat_barred_from_shell_git_mutation_cannot_mutate_git_through_mcp(
    temp_db: HubDatabase, name: str
) -> None:
    engine = _seat_git_engine(temp_db, name)
    session = _mcp_call(*READ_ONLY_MCP_CALL).session_id

    decisions = {
        f"{server}:{tool}": (
            await engine.evaluate(_mcp_call(server, tool), session_id=session, variables={})
        ).decision
        for server, tool in sorted(GIT_MUTATING_MCP_TOOLS)
    }
    read_only = await engine.evaluate(
        _mcp_call(*READ_ONLY_MCP_CALL), session_id=session, variables={}
    )

    assert decisions == dict.fromkeys(decisions, "block"), name
    assert read_only.decision == "allow", read_only.reason
