"""A devteam re-run with team_id launches only the team's missing seats (#24958)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_models import Session
from gobby.storage.tasks import Task
from gobby.storage.workspaces import WorkspaceManager
from gobby.utils.machine_id import require_machine_id
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_executor import step_invocation_id
from gobby.workflows.templates import TemplateEngine
from tests.agents._yaml_helpers import flat
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import TITLE, TeamWorld, execution_id, make_team_world, running_seat

pytestmark = pytest.mark.unit

DEVTEAM = PipelineDefinition.model_validate(
    yaml.safe_load(
        (
            Path(__file__).resolve().parents[2]
            / "src/gobby/install/shared/workflows/pipelines/devteam.yaml"
        ).read_text()
    )
)
SEATS = ("dev-manager", "test-writer", "developer", "code-reviewer")
RENDERER = StepRenderer(TemplateEngine())


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
    definitions = AgentDefinitionManager(world.case.db)
    for name in SEATS:
        definitions.create(
            name=name,
            definition_json={"name": name, "version": "1.0", "enabled": True},
            project_id=world.case.project_id,
            enabled=True,
        )
    return world


def _inputs(task: Task, workspace: str, report_to: str, team_id: str | None) -> dict[str, Any]:
    values = {name: spec.get("default") for name, spec in DEVTEAM.inputs.items()}
    values.update(task=task.id, workspace=workspace, report_to=report_to, team_id=team_id)
    return values


def _execution(world: TeamWorld, task: Task, inputs: dict[str, Any]) -> Session:
    """A running devteam execution for ``task`` whose stored launch inputs are ``inputs``."""
    run = world.child(task, DEVTEAM)
    stored = {key: value for key, value in inputs.items() if value is not None}
    world.case.db.execute(
        "UPDATE pipeline_executions SET inputs_json = %s WHERE id = %s",
        (json.dumps(stored), execution_id(run)),
    )
    return run


def _step_context(run: Session, step_id: str, context: dict[str, Any]) -> dict[str, Any]:
    return {**context, "invocation_id": step_invocation_id(execution_id(run), step_id)}


async def _call(world: TeamWorld, run: Session, step_id: str, context: dict[str, Any]) -> None:
    """Run ``run``'s rendered ``step_id`` MCP step; the context then holds its output."""
    step = next(step for step in DEVTEAM.steps if step.id == step_id)
    assert step.mcp is not None and step.mcp.arguments is not None
    arguments = RENDERER.render_mcp_arguments(
        step.mcp.arguments, _step_context(run, step_id, context), drop_none=True
    )
    with session_context_for_test(run.id):
        reply = cast(dict[str, Any], await world.tools.call(step.mcp.tool, arguments))
    assert reply.get("success") is True, reply
    context["steps"][step_id] = {"output": reply}


async def _admit(world: TeamWorld, run: Session, inputs: dict[str, Any]) -> dict[str, Any]:
    """Run ``run``'s guard and admit steps as its executor does; return the step context."""
    context: dict[str, Any] = {
        "inputs": inputs,
        "steps": {},
        "_string_inputs": frozenset(
            name for name, spec in DEVTEAM.inputs.items() if spec.get("type") == "string"
        ),
    }
    await _call(world, run, "guard", context)
    await _call(world, run, "admit", context)
    return context


def _launched(run: Session, context: dict[str, Any]) -> list[str]:
    """The seat steps that run in ``context``."""
    return [
        step.id
        for step in DEVTEAM.steps
        if step.id in SEATS and RENDERER.should_run_step(step, _step_context(run, step.id, context))
    ]


def _seat_prompt(run: Session, step_id: str, context: dict[str, Any]) -> str:
    """The prompt ``step_id``'s spawn launches with, rendered from ``context``."""
    step = next(step for step in DEVTEAM.steps if step.id == step_id)
    assert step.mcp is not None and step.mcp.arguments is not None
    arguments = RENDERER.render_mcp_arguments(
        step.mcp.arguments, _step_context(run, step_id, context), drop_none=True
    )
    return str(arguments["prompt"])


def _names_dev_manager(prompt: str, ref: str) -> bool:
    """Whether one sentence of ``prompt`` gives ``ref`` as the Dev Manager's target="session"."""
    return any(
        ref in sentence
        and "dev manager" in sentence.lower()
        and re.search(r"target=[\"']session[\"']", sentence) is not None
        for sentence in re.split(r"(?<=\.)\s+", flat(prompt))
    )


async def _launch(world: TeamWorld, run: Session, inputs: dict[str, Any]) -> list[str]:
    """Run ``run``'s guard and admit steps as its executor does; return the seat steps that run."""
    return _launched(run, await _admit(world, run, inputs))


async def _team_with_live(
    world: TeamWorld, task: Task, workspace: str, roles: tuple[str, ...]
) -> str:
    """Launch a devteam for ``task``, leave ``roles`` live and finish it; return the team id."""
    original = _execution(world, task, _inputs(task, workspace, world.case.creator, None))
    team = await world.admit(original, task)
    for role in roles:
        seat = running_seat(world, original, role)
        world.case.db.execute(
            "UPDATE agent_runs SET task_id = %s WHERE id = %s", (task.id, seat.agent_run_id)
        )
    world.finish(original)
    return team.team_id


@pytest.mark.asyncio
@pytest.mark.parametrize("uppercase_team_id", [False, True])
async def test_rerun_with_team_id_launches_only_the_missing_seats(
    world: TeamWorld, uppercase_team_id: bool
) -> None:
    """Live seats come from the team's own runs: no tab exists and other teams' seats don't count."""
    workspace = WorkspaceManager(world.case.db).create(require_machine_id(), "devteam")[0].id
    task, other = world.case.task(TITLE), world.case.task("Another team task")
    team_id = await _team_with_live(
        world, task, workspace, ("dev-manager", "test-writer", "code-reviewer")
    )
    other_team_id = await _team_with_live(world, other, workspace, ("developer",))
    if uppercase_team_id:
        team_id = team_id.upper()
        other_team_id = other_team_id.upper()

    rerun = _inputs(task, workspace, world.case.creator, team_id)
    other_rerun = _inputs(other, workspace, world.case.creator, other_team_id)
    launched = await _launch(world, _execution(world, task, rerun), rerun)
    other_launched = await _launch(world, _execution(world, other, other_rerun), other_rerun)

    assert launched == ["developer"]
    assert other_launched == ["dev-manager", "test-writer", "code-reviewer"]


@pytest.mark.asyncio
async def test_fresh_team_seats_name_the_spawned_dev_manager_session(world: TeamWorld) -> None:
    """A new team's later seats name the Dev Manager's session from its spawn reply.

    The spawn reply is hand-built to the ``child_session_id`` shape spawn_agent returns.
    """
    workspace = WorkspaceManager(world.case.db).create(require_machine_id(), "devteam")[0].id
    task = world.case.task(TITLE)
    inputs = _inputs(task, workspace, world.case.creator, None)
    run = _execution(world, task, inputs)
    context = await _admit(world, run, inputs)
    dev_manager = "5f1c0e8e-3b7a-4c51-9d2e-0a6b8c7d1e2f"
    context["steps"]["dev-manager"] = {"output": {"success": True, "child_session_id": dev_manager}}

    assert _launched(run, context) == [
        "dev-manager",
        "test-writer",
        "developer",
        "code-reviewer",
    ]
    for seat in ("test-writer", "developer", "code-reviewer"):
        assert _names_dev_manager(_seat_prompt(run, seat, context), dev_manager), seat


@pytest.mark.asyncio
async def test_relaunched_seats_name_the_live_dev_manager_session(world: TeamWorld) -> None:
    """With the Dev Manager still live, each seat a re-run launches names its session.

    The live session comes from the guard's ``live`` map, where a skipped Dev Manager is known.
    """
    workspace = WorkspaceManager(world.case.db).create(require_machine_id(), "devteam")[0].id
    task = world.case.task(TITLE)
    team_id = await _team_with_live(world, task, workspace, ("dev-manager",))
    rerun = _inputs(task, workspace, world.case.creator, team_id)
    run = _execution(world, task, rerun)
    context = await _admit(world, run, rerun)
    dev_manager = context["steps"]["guard"]["output"]["live"]["dev-manager"]

    launched = _launched(run, context)
    assert launched == ["test-writer", "developer", "code-reviewer"]
    for seat in launched:
        assert _names_dev_manager(_seat_prompt(run, seat, context), dev_manager), seat
