"""Runbook seat and tab titles, from launch through claim, close and resume.

Pane and session titles are the agent definition display name: the agent name
title-cased (dev-manager -> Dev Manager), the convention apply_agent_definition pins.
A task-bound runbook opens '<Runbook> - #<task>'; a runbook without a task opens its
runbook name. Static operator tabs keep fixed names. Contract: docs/contracts/seat-titles.md.
"""

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import yaml

from gobby.agents.session import ChildSessionManager
from gobby.agents.spawn import prepare_terminal_resume, prepare_terminal_spawn
from gobby.mcp_proxy.tools.spawn_agent import _factory
from gobby.sessions.title_lifecycle import recompute_automatic_title, update_title_for_claim
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.machines import LocalMachineManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.storage.terminals import TerminalManager
from gobby.storage.workspaces import WorkspaceManager
from gobby.terminals.workspace_agent_panes import AgentPaneReserver, AgentPlacement
from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.templates import TemplateEngine
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX, TEST_USER_ID
from tests.terminals.fakes import FakeRuntime, runtime_registry

PIPELINES = Path(__file__).resolve().parents[2] / "src/gobby/install/shared/workflows/pipelines"
BRANCH = "task-24797-runbooks"
TASK_REF = "#22604"
EXPECTED_TAB = {
    "devteam": f"Dev Team - {TASK_REF}",
    "support": "Support",
    "team-planning": "Team Planning",
    "research": "Research",
}


@pytest.mark.parametrize(
    ("tab_title", "seat_title"),
    [
        pytest.param(f"Dev Team - {TASK_REF}", "Dev Manager", id="devteam"),
        pytest.param("Support", "Release Manager", id="support"),
        pytest.param(f"Research - {TASK_REF}", "Researcher", id="research"),
        pytest.param(f"Team Planning - {TASK_REF}", "Plan Writer", id="team-planning"),
    ],
)
@pytest.mark.asyncio
async def test_tab_opening_seat_keeps_its_definition_pane_title(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tab_title: str,
    seat_title: str,
) -> None:
    """Reservation preserves the runbook tab title and gives its opener a seat title.

    Research exercises the shared placement path while its pipeline YAML is unlanded.
    """
    db = session_manager.db
    machine_id = f"{TEST_MACHINE_ID_PREFIX}000000000001"
    monkeypatch.setattr("gobby.utils.machine_id._cached_machine_id", machine_id)
    LocalMachineManager(db).upsert_seen(machine_id, TEST_USER_ID, hostname="local")
    workspaces = WorkspaceManager(db)
    workspace = workspaces.create(machine_id, "seat-titles")[0]
    reserver = AgentPaneReserver(
        workspaces=workspaces,
        terminals=TerminalManager(db),
        registry=runtime_registry(FakeRuntime(backend="tmux"), FakeRuntime(backend="native")),
        sessions=session_manager,
        publish=AsyncMock(),
    )
    placement = AgentPlacement.parse(
        {"tab": {"workspace": workspace.id, "title": tab_title}}
    ).for_seat(seat_title)

    resolved = await reserver.preflight("operator", sample_project["id"], placement)
    reserved = await reserver.reserve(resolved, worktree_id=None)

    tab = next(tab for tab in workspaces.list_tabs(workspace.id) if tab.id == reserved.tab_id)
    pane = next(pane for pane in workspaces.list_panes(workspace.id) if pane.id == reserved.pane_id)
    assert tab.title == tab_title
    assert pane.label == seat_title


def _placement_titles(placement: dict[str, Any]) -> tuple[str | None, str]:
    """Return (pane title, tab title). A tab placement leaves the pane to the session title."""
    if "split" in placement:
        return placement["split"]["title"], placement["split"]["tab_title"]
    return None, placement["tab"]["title"]


@pytest.mark.parametrize(
    ("runbook", "seat", "pane_title"),
    [
        ("devteam", "dev-manager", "Dev Manager"),
        ("devteam", "test-writer", "Test Writer"),
        ("devteam", "developer", "Developer"),
        ("devteam", "code-reviewer", "Code Reviewer"),
        ("support", "release-manager", "Release Manager"),
        ("support", "triage", "Triage"),
        ("support", "archivist", "Archivist"),
        ("support", "security-reviewer", "Security Reviewer"),
        ("support", "recovery-steward", "Recovery Steward"),
        ("support", "consolidator", "Consolidator"),
        ("team-planning", "plan-writer", "Plan Writer"),
        ("team-planning", "plan-adversary", "Plan Adversary"),
        ("team-planning", "plan-enhancer", "Plan Enhancer"),
        ("research", "researcher", "Researcher"),
        ("research", "research-reviewer", "Research Reviewer"),
    ],
)
@pytest.mark.asyncio
@pytest.mark.parametrize("team_id", [None, "00000000-0000-4000-8000-0000000000aa"])
async def test_runbook_seat_titles_are_definition_names_through_claim(
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    runbook: str,
    seat: str,
    pane_title: str,
    team_id: str | None,
) -> None:
    """Seat panes and sessions carry the definition name; the tab carries the runbook.

    Contract: docs/contracts/seat-titles.md, rules 1 and 2. Task text, slugs and
    caller-supplied titles never reach a seat's pane or session title.
    """
    tab_title = EXPECTED_TAB[runbook]
    definition = PipelineDefinition.model_validate(
        yaml.safe_load((PIPELINES / f"{runbook}.yaml").read_text())
    )
    assert [name for name in definition.inputs if "lane" in name] == []
    inputs = {name: spec.get("default") for name, spec in definition.inputs.items()}
    inputs.update(
        task=TASK_REF,
        team_id=team_id,
        seats=seat,
        workspace="test-workspace",
        project_path=str(PIPELINES),
        report_to="gobby#1",
    )
    step = next(step for step in definition.steps if step.id == seat)
    assert step.mcp is not None and step.mcp.arguments is not None
    renderer = StepRenderer(TemplateEngine())
    arguments = renderer.render_mcp_arguments(
        step.mcp.arguments,
        {
            "inputs": inputs,
            "_string_inputs": frozenset(
                name for name, spec in definition.inputs.items() if spec.get("type") == "string"
            ),
            "invocation_id": str(uuid4()),
            "steps": {
                **{
                    step.id: {"output": {"pane_ref": "test-pane", "child_session_id": str(uuid4())}}
                    for step in definition.steps
                },
                "guard": {
                    "output": {
                        "worktree": str(uuid4()),
                        "project_path": str(PIPELINES),
                        "report_to": "gobby#1",
                        "workspace_id": "test-workspace",
                        "launch": [seat],
                        "live": {},
                        "tab_exists": False,
                        "tab_title": EXPECTED_TAB[runbook],
                    }
                },
                "admit": {
                    "output": {
                        "branch": BRANCH,
                        "project_path": str(PIPELINES),
                        "task_id": None,
                        "team": {"team_id": str(uuid4())},
                    }
                },
            },
        },
        drop_none=True,
    )
    parent = session_manager.register(
        external_id=f"parent-{runbook}-{seat}",
        machine_id="21000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
    )
    title_listener = Mock()
    session_manager.register_title_listener(title_listener)

    async def launch(**kwargs: Any) -> dict[str, Any]:
        prepared = prepare_terminal_spawn(
            session_manager=ChildSessionManager(session_manager),
            parent_session_id=parent.id,
            project_id=parent.project_id,
            machine_id=parent.machine_id,
            source="codex",
            agent_name=kwargs["agent_lookup_name"],
            initial_variables=kwargs["initial_variables"],
        )
        return {"success": True, "prepared": prepared, "variables": kwargs["initial_variables"]}

    from gobby.workflows.agent_models import (
        AgentDefinitionBody,
        AgentPromptBlocks,
        AgentSelector,
        AgentWorkflows,
    )

    monkeypatch.setattr(
        _factory,
        "_load_agent_body",
        Mock(
            return_value=AgentDefinitionBody(
                name=seat,
                prompts=AgentPromptBlocks(agent="Runbook seat"),
                workflows=AgentWorkflows(rule_selectors=AgentSelector()),
            )
        ),
    )
    monkeypatch.setattr(_factory, "enforce_spawn_caller", AsyncMock(return_value=parent.id))
    monkeypatch.setattr(_factory, "pipeline_caller", Mock(return_value=parent))
    monkeypatch.setattr(_factory, "pipeline_invocation_reply", Mock(return_value=None))
    monkeypatch.setattr(_factory, "spawn_agent_impl", launch)
    registry = _factory.create_spawn_agent_registry(
        Mock(), session_manager=session_manager, db=session_manager.db
    )
    placement = arguments.get("placement")
    assert isinstance(placement, dict)
    pane, tab = _placement_titles(placement)
    assert tab == tab_title
    assert pane in (None, pane_title)

    result = await registry.call("spawn_agent", arguments)
    assert result["success"]
    prepared = result["prepared"]
    variables = result["variables"]

    child = session_manager.get(prepared.session_id)
    assert child is not None
    assert child.title == pane_title and child.title_source == "definition"
    title_listener.assert_called_with(child.id, pane_title)
    tasks = LocalTaskManager(session_manager.db)
    task = tasks.create_task(
        parent.project_id,
        "Temporary seat work",
        claimed_by_session_id=child.id,
        validation_criteria="The seat title stays the definition name while the claim is open.",
    )
    claimed = update_title_for_claim(session_manager, child.id, task)
    assert claimed is not None
    assert claimed.title == pane_title and claimed.title_source == "definition"

    LocalAgentRunManager(session_manager.db).complete(prepared.agent_run_id)
    session_manager.update_status(child.id, "expired")
    resumed = prepare_terminal_resume(
        session_manager=ChildSessionManager(session_manager),
        existing_session_id=child.id,
        original_run_id=prepared.agent_run_id,
        parent_session_id=parent.id,
        project_id=parent.project_id,
        source="codex",
        workflow_name=None,
        agent_name=seat,
        initial_variables=variables,
        git_branch=None,
        prompt="",
        model=None,
        is_local=False,
        max_agent_depth=1,
        agent_run_id=str(uuid4()),
        task_id=task.id,
        claimed_session_id=child.id,
        timeout_seconds=None,
        sandbox_enabled=False,
        requested_reasoning_effort=None,
        effective_reasoning_effort=None,
        reasoning_required=False,
        reasoning_status="not_requested",
        reasoning_message=None,
        resume_metadata_json={},
        worktree_id=None,
        clone_id=None,
        workspace_path=str(PIPELINES),
        relaunch=True,
    )
    assert resumed.session_id == child.id
    after_resume = session_manager.get(child.id)
    assert after_resume is not None and after_resume.title == claimed.title

    tasks.close_task(task.id, force=True, closed_in_session_id=child.id)
    restored = recompute_automatic_title(session_manager, child.id)

    assert restored.title == pane_title and restored.title_source == "definition"
    title_listener.assert_called_with(child.id, pane_title)
