"""Epic-mode procedures and the devteam runbook binding (addendum 8.5).

The epic-mode contract lives in the seat procedures and definitions; the devteam
runbook binds either an epic or a leaf with one seat catalogue (Decision 15).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from gobby.mcp_proxy.tools.spawn_agent import _factory
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.agent_models import (
    AgentDefinitionBody,
    AgentPromptBlocks,
    AgentSelector,
    AgentWorkflows,
)
from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_executor import step_invocation_id
from gobby.workflows.templates import TemplateEngine
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import SLUG, TITLE, TeamWorld, execution_id, make_team_world

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SHARED = ROOT / "src/gobby/install/shared"
DEVTEAM_PATH = SHARED / "workflows/pipelines/devteam.yaml"
DEVTEAM = PipelineDefinition.model_validate(yaml.safe_load(DEVTEAM_PATH.read_text()))
RENDERER = StepRenderer(TemplateEngine())
WORKSPACE = "pilot-workspace-id"


@pytest.fixture
def world(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TeamWorld:
    world = make_team_world(
        make_landing_case(temp_db, sample_project, tmp_path), tmp_path, monkeypatch
    )

    def seat_body(name: str, *args: Any, **kwargs: Any) -> AgentDefinitionBody:
        return AgentDefinitionBody(
            name=name,
            prompts=AgentPromptBlocks(agent="Runbook seat"),
            workflows=AgentWorkflows(rule_selectors=AgentSelector()),
        )

    # admit_team binds only an enabled reviewer definition with the review grant.
    AgentDefinitionManager(temp_db).create(
        "code-reviewer",
        {
            "name": "code-reviewer",
            "version": "1.0",
            "prompts": {"agent": "Review the leaf."},
            "workflows": {"rule_selectors": {"include": []}},
            "task_step_writes": ["review"],
        },
        project_id=world.case.project_id,
    )
    monkeypatch.setattr(_factory, "_load_agent_body", seat_body)
    return world


def _context(inputs: dict[str, Any]) -> dict[str, Any]:
    values = {name: spec.get("default") for name, spec in DEVTEAM.inputs.items()}
    values.update(workspace="pilot-workspace", report_to="gobby#14972", **inputs)
    return {
        "inputs": values,
        # A fresh dry run has no live seats; the seat prompts read this map unconditionally.
        "steps": {
            # The guard resolves the tab title from the task; this fixture's task is #25128.
            "guard": {
                "output": {
                    "workspace_id": WORKSPACE,
                    "live": {},
                    "tab_exists": False,
                    "tab_title": "Dev Team - #25128",
                }
            }
        },
        "_string_inputs": frozenset(
            name for name, spec in DEVTEAM.inputs.items() if spec.get("type") == "string"
        ),
    }


def _render(run: Session, step_id: str, context: dict[str, Any]) -> dict[str, Any] | None:
    step = next(step for step in DEVTEAM.steps if step.id == step_id)
    step_context = {**context, "invocation_id": step_invocation_id(execution_id(run), step_id)}
    if not RENDERER.should_run_step(step, step_context):
        return None
    assert step.mcp is not None and step.mcp.arguments is not None
    return RENDERER.render_mcp_arguments(step.mcp.arguments, step_context, drop_none=True)


async def _dry_run(world: TeamWorld, task: Task) -> tuple[dict[str, Any], dict[str, str]]:
    """Admit ``task`` through the rendered admit step; return its reply and launched seats."""
    run = world.child(task, DEVTEAM)
    context = _context({"task": task.id, "reviewer_agent": "code-reviewer"})
    arguments = _render(run, "admit", context)
    assert arguments is not None
    with session_context_for_test(run.id):
        reply = cast(dict[str, Any], await world.tools.call("admit_team", arguments))
    assert reply.get("success") is True, reply
    context["steps"]["admit"] = {"output": reply}
    # A fresh team launches every seat; check_runbook_seats reports them as ``launch``.
    context["steps"]["guard"]["output"]["launch"] = [
        "dev-manager",
        "test-writer",
        "developer",
        "code-reviewer",
    ]
    seats: dict[str, str] = {}
    for step in DEVTEAM.steps:
        rendered = _render(run, step.id, context)
        if rendered is not None and step.mcp is not None and step.mcp.tool == "spawn_agent":
            seats[step.id] = rendered["agent"]
            # The spawn reply a later step reads: the seat's child session and its pane.
            context["steps"][step.id] = {
                "output": {"child_session_id": f"child-{step.id}", "pane_ref": f"pane-{step.id}"}
            }
    return reply, seats


async def test_runbook_binds_epic_or_leaf(world: TeamWorld) -> None:
    tasks = LocalTaskManager(world.case.db)
    project_id = world.case.project_id
    epic = tasks.create_task(
        project_id, TITLE, task_type="epic", created_in_session_id=world.case.creator
    )
    tasks.create_task(
        project_id,
        "Wire the outage banner",
        parent_task_id=epic.id,
        created_in_session_id=world.case.creator,
        validation_criteria="The banner renders.",
    )
    leaf = world.case.task("Standalone leaf task")

    epic_reply, epic_seats = await _dry_run(world, epic)
    leaf_reply, leaf_seats = await _dry_run(world, leaf)

    # Decision 18: the epic branch and its titled worktree; a leaf keeps the spawn default.
    assert epic_reply["branch"] == f"epic/{epic.seq_num}/main"
    assert Path(epic_reply["project_path"]).name == f"wt-epic-{epic.seq_num}-{SLUG}"
    assert leaf_reply["branch"].startswith(f"task-{leaf.seq_num}-")
    # One runbook, one seat catalogue, whichever task it binds.
    assert epic_seats == leaf_seats
    assert sorted(epic_seats.values()) == [
        "code-reviewer",
        "dev-manager",
        "developer",
        "test-writer",
    ]


def _seat_arguments(seat: str, context: dict[str, Any]) -> dict[str, Any]:
    step = next(step for step in DEVTEAM.steps if step.id == seat)
    assert step.mcp is not None and step.mcp.arguments is not None
    return RENDERER.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)


def _placement_context(launch: list[str], tab_exists: bool) -> dict[str, Any]:
    """A replacement run: team_id names the team, and the guard reports the team tab."""
    context = _context({"task": "#25128", "team_id": "team-1"})
    context["invocation_id"] = "placement-test"
    context["steps"]["guard"]["output"].update(launch=launch, tab_exists=tab_exists)
    context["steps"]["admit"] = {
        "output": {
            "branch": "epic/25128/main",
            "project_path": "/pilot/checkout",
            "task_id": None,
            "team": {"team_id": "team-1"},
        }
    }
    return context


@pytest.mark.parametrize("task", ["#25128", "25128", "00000000-0000-4000-8000-000000025128"])
def test_seat_tabs_take_the_guards_sequence_title_for_any_task_form(task: str) -> None:
    """Placements read the guard's resolved tab title, so every task form opens 'Dev Team - #25128'."""
    context = _placement_context(launch=["dev-manager"], tab_exists=False)
    context["inputs"]["task"] = task
    context["steps"]["guard"]["output"]["tab_title"] = "Dev Team - #25128"
    placement = _seat_arguments("dev-manager", context)["placement"]
    assert placement == {"tab": {"workspace": WORKSPACE, "title": "Dev Team - #25128"}}


@pytest.mark.parametrize("tab_exists", [False, True])
def test_manager_opens_the_team_tab_when_none_exists(tab_exists: bool) -> None:
    """A replacement named by team_id still opens the team tab when that tab is missing."""
    context = _placement_context(launch=["dev-manager"], tab_exists=tab_exists)
    placement = _seat_arguments("dev-manager", context)["placement"]
    if tab_exists:
        assert placement == {
            "split": {
                "workspace": WORKSPACE,
                "tab_title": "Dev Team - #25128",
                "axis": "right",
                "title": "Dev Manager",
            }
        }
    else:
        assert placement == {"tab": {"workspace": WORKSPACE, "title": "Dev Team - #25128"}}


@pytest.mark.parametrize(
    ("launch", "tab_exists", "splits"),
    [
        (["dev-manager", "test-writer"], False, True),  # the manager opens the tab first
        (["test-writer"], False, False),  # nothing launched in this run opens it
        (["test-writer"], True, True),  # the team tab exists
    ],
)
def test_seats_open_the_team_tab_only_when_nothing_opens_it(
    launch: list[str], tab_exists: bool, splits: bool
) -> None:
    context = _placement_context(launch=launch, tab_exists=tab_exists)
    placement = _seat_arguments("test-writer", context)["placement"]
    assert ("split" in placement) is splits


def _arrange_steps() -> list[str]:
    return [
        step.id
        for step in DEVTEAM.steps
        if step.mcp is not None and step.mcp.tool == "rebalance_tab"
    ]


def test_devteam_tiles_the_team_tab_after_the_last_seat() -> None:
    """One arrange step tiles the team tab once every seat has been launched."""
    arrange = _arrange_steps()
    assert len(arrange) == 1, arrange
    order = [step.id for step in DEVTEAM.steps]
    seats = [
        step.id for step in DEVTEAM.steps if step.mcp is not None and step.mcp.tool == "spawn_agent"
    ]
    assert seats, order
    assert order.index(arrange[0]) > max(order.index(seat) for seat in seats)


@pytest.mark.parametrize(
    "launch",
    [
        ["dev-manager", "test-writer", "developer", "code-reviewer"],  # a fresh team
        ["code-reviewer"],  # a re-run adds the one missing seat
    ],
    ids=["fresh-team", "re-run-adds-a-seat"],
)
def test_devteam_tab_stays_tiled_after_any_seat_launch(launch: list[str]) -> None:
    arrange = _arrange_steps()
    assert len(arrange) == 1, arrange
    step = next(step for step in DEVTEAM.steps if step.id == arrange[0])
    context = _placement_context(launch=launch, tab_exists=True)
    for seat in launch:
        context["steps"][seat] = {"output": {"pane_ref": f"pane-{seat}"}}
    assert RENDERER.should_run_step(step, context), launch
    assert step.mcp is not None and step.mcp.arguments is not None
    arguments = RENDERER.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)
    assert arguments.get("layout") == "tiled", arguments
    assert "Dev Team - #25128" in str(arguments.get("tab")), arguments


def _pipeline(name: str) -> PipelineDefinition:
    return PipelineDefinition.model_validate(
        yaml.safe_load((SHARED / f"workflows/pipelines/{name}.yaml").read_text())
    )


@pytest.mark.parametrize(
    ("name", "tab"),
    [("support", "Support")],
)
@pytest.mark.parametrize("launch", [["seat"], []], ids=["seats-launched", "nothing-launched"])
def test_shared_tab_runbooks_tile_their_tab_after_the_last_seat(
    name: str, tab: str, launch: list[str]
) -> None:
    pipeline = _pipeline(name)
    arrange = [
        step for step in pipeline.steps if step.mcp is not None and step.mcp.tool == "rebalance_tab"
    ]
    assert len(arrange) == 1, arrange
    order = [step.id for step in pipeline.steps]
    seats = [
        step.id
        for step in pipeline.steps
        if step.mcp is not None and step.mcp.tool == "spawn_agent"
    ]
    assert order.index(arrange[0].id) > max(order.index(seat) for seat in seats)
    context = {
        "inputs": {"workspace": "pilot-workspace"},
        "steps": {"guard": {"output": {"workspace_id": WORKSPACE, "launch": launch}}},
        "_string_inputs": frozenset({"workspace"}),
    }
    step = arrange[0]
    assert RENDERER.should_run_step(step, context) is bool(launch)
    assert step.mcp is not None and step.mcp.arguments is not None
    arguments = RENDERER.render_mcp_arguments(step.mcp.arguments, context, drop_none=True)
    assert arguments == {
        "workspace": "pilot-workspace",
        "tab": tab,
        "rows": 100,
        "layout": "tiled",
    }, arguments


def test_team_planning_tiles_the_policy_tab_its_writer_opens() -> None:
    """Planning opens 'Team Planning - #<task>', as project_policy names it, and tiles that tab."""
    pipeline = _pipeline("team-planning")
    assert "task" in pipeline.inputs
    steps = {step.id: step for step in pipeline.steps}
    guard = steps["guard"].mcp
    assert guard is not None and guard.arguments is not None
    assert guard.arguments["task"] == "${{ inputs.task }}"
    assert guard.arguments["tab_title"] == "Team Planning"
    title = "Team Planning - #25186"
    tab_ref = f"pilot-node:{WORKSPACE}:tab-1"
    context = {
        "inputs": {
            "workspace": "pilot-workspace",
            "task": "#25186",
            "seat_pane": None,
            "seats": "plan-writer",
        },
        "steps": {
            "guard": {
                "output": {
                    "workspace_id": WORKSPACE,
                    "launch": ["plan-writer"],
                    "tab_title": title,
                    "tab_exists": False,
                }
            },
            "plan-writer": {"output": {"tab_ref": tab_ref, "pane_ref": f"{tab_ref}:pane-1"}},
        },
        "_string_inputs": frozenset({"workspace", "task"}),
    }
    arrange = steps["arrange"].mcp
    assert arrange is not None and arrange.arguments is not None
    arranged = RENDERER.render_mcp_arguments(arrange.arguments, context, drop_none=True)
    assert arranged == {
        "tab": tab_ref,
        "seats": [{"name": "plan-writer", "pane": f"{tab_ref}:pane-1"}],
        "rows": 100,
        "layout": "tiled",
    }
    writer = steps["plan-writer"].mcp
    assert writer is not None and writer.arguments is not None
    placement = RENDERER.render_mcp_arguments(
        {"placement": writer.arguments["placement"]}, context, drop_none=True
    )
    assert placement == {"placement": {"tab": {"workspace": "pilot-workspace", "title": title}}}
