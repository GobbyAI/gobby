"""The declared developer/reviewer pair admits one checkout and explicit claim ownership."""

from __future__ import annotations

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
from tests.fixtures.agent_definitions import make_agent_definition
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import TITLE, TeamWorld, execution_id, make_team_world

pytestmark = pytest.mark.unit
DEVTEAM = PipelineDefinition.model_validate(
    yaml.safe_load(
        (
            Path(__file__).resolve().parents[2]
            / "src/gobby/install/shared/workflows/pipelines/devteam.yaml"
        ).read_text()
    )
)
RENDERER = StepRenderer(TemplateEngine())
TEST_MODEL = "claude-test-model"


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
    for name in ("developer", "code-reviewer"):
        AgentDefinitionManager(temp_db).create(
            name,
            make_agent_definition(
                name=name, prompts={"agent": "Perform the assigned work."}
            ).model_dump(mode="json"),
            project_id=world.case.project_id,
        )
    return world


def _inputs(world: TeamWorld, task: Task) -> dict[str, Any]:
    workspace = WorkspaceManager(world.case.db).create(require_machine_id(), "dev-pair")[0].id
    values = {name: spec.get("default") for name, spec in DEVTEAM.inputs.items()}
    values.update(
        task=task.id,
        workspace=workspace,
        report_to=world.case.creator,
        developer_model=TEST_MODEL,
        reviewer_model=TEST_MODEL,
    )
    return values


def _arguments(run: Session, step_id: str, context: dict[str, Any]) -> dict[str, Any]:
    step = next(step for step in DEVTEAM.steps if step.id == step_id)
    assert step.mcp is not None and step.mcp.arguments is not None
    return RENDERER.render_mcp_arguments(
        step.mcp.arguments,
        {**context, "invocation_id": step_invocation_id(execution_id(run), step_id)},
        drop_none=True,
    )


async def _call(world: TeamWorld, run: Session, step_id: str, context: dict[str, Any]) -> None:
    step = next(step for step in DEVTEAM.steps if step.id == step_id)
    assert step.mcp is not None
    with session_context_for_test(run.id):
        reply = cast(
            dict[str, Any], await world.tools.call(step.mcp.tool, _arguments(run, step_id, context))
        )
    assert reply.get("success") is True, reply
    context["steps"][step_id] = {"output": reply}


@pytest.mark.asyncio
async def test_pair_admission_supplies_both_seats_one_checkout_and_one_claim(
    world: TeamWorld,
) -> None:
    task = world.case.task(TITLE)
    run = world.child(task, DEVTEAM)
    context: dict[str, Any] = {"inputs": _inputs(world, task), "steps": {}}

    await _call(world, run, "guard", context)
    await _call(world, run, "admit", context)
    developer = _arguments(run, "developer", context)
    context["steps"]["developer"] = {"output": {"child_session_id": "developer-session"}}
    reviewer = _arguments(run, "reviewer", context)

    admitted = context["steps"]["admit"]["output"]
    assert developer["task_id"] == task.id
    assert reviewer["task_id"] == task.id
    assert reviewer["claim"] is False
    assert developer["project_path"] == reviewer["project_path"] == admitted["project_path"]
    assert Path(admitted["project_path"]).is_dir()
    assert developer["checkout_mode"] == reviewer["checkout_mode"] == "none"
    assert developer["reserved_run_id"] != reviewer["reserved_run_id"]
    assert developer["reserved_run_id"] == step_invocation_id(execution_id(run), "developer")
    assert reviewer["reserved_run_id"] == step_invocation_id(execution_id(run), "reviewer")


@pytest.mark.asyncio
async def test_pair_connect_names_the_spawned_reviewer_and_declared_report_target(
    world: TeamWorld,
) -> None:
    task = world.case.task(TITLE)
    run = world.child(task, DEVTEAM)
    inputs = _inputs(world, task)
    context: dict[str, Any] = {"inputs": inputs, "steps": {}}
    await _call(world, run, "guard", context)
    await _call(world, run, "admit", context)
    context["steps"].update(
        developer={"output": {"child_session_id": "developer-session"}},
        reviewer={"output": {"child_session_id": "reviewer-session"}},
    )

    connect = _arguments(run, "connect", context)

    assert connect["target_id"] == "developer-session"
    assert "reviewer-session" in connect["content"]
    assert inputs["report_to"] in _arguments(run, "developer", context)["prompt"]
    assert inputs["report_to"] in _arguments(run, "reviewer", context)["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("seat", ["developer", "reviewer"])
async def test_pair_guard_refuses_a_missing_model_before_creating_a_tree(
    world: TeamWorld, seat: str
) -> None:
    task = world.case.task(TITLE)
    run = world.child(task, DEVTEAM)
    inputs = _inputs(world, task)
    inputs[f"{seat}_model"] = None
    context: dict[str, Any] = {"inputs": inputs, "steps": {}}
    arguments = _arguments(run, "guard", context)

    with session_context_for_test(run.id):
        reply = cast(dict[str, Any], await world.tools.call("check_runbook_seats", arguments))

    assert reply["success"] is False
    assert "model" in str(reply["error"])
    assert world.task_branches() == []
    assert world.tree_ids() == []
