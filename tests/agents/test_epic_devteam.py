"""The epic runbook admits a persistent team and renders assigned, unclaimed seats."""

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.definitions import PipelineDefinition
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_executor import step_invocation_id
from gobby.workflows.templates import TemplateEngine
from tests.agents.test_devteam_rerun import _inputs
from tests.fixtures.agent_definitions import make_agent_definition
from tests.fixtures.landing_case import make_landing_case
from tests.fixtures.team_case import TeamWorld, execution_id, make_team_world

pytestmark = pytest.mark.unit


@pytest.fixture
def world(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> TeamWorld:
    result = make_team_world(
        make_landing_case(temp_db, sample_project, tmp_path), tmp_path, monkeypatch
    )
    for name in ("developer", "code-reviewer", "dev-manager"):
        AgentDefinitionManager(temp_db).create(
            name,
            make_agent_definition(name=name, prompts={"agent": "Perform work"}).model_dump(
                mode="json"
            ),
            project_id=result.case.project_id,
        )
    return result


@pytest.mark.asyncio
async def test_epic_seats_use_assignment_without_claim(
    world: TeamWorld, temp_db: HubDatabase
) -> None:
    path = (
        Path(__file__).parents[2] / "src/gobby/install/shared/workflows/pipelines/epic-devteam.yaml"
    )
    assert path.is_file(), "epic-devteam must be available as a runbook"
    pipeline = PipelineDefinition.model_validate(yaml.safe_load(path.read_text()))
    task = LocalTaskManager(temp_db).create_task(
        world.case.project_id, "Persistent epic", task_type="epic"
    )
    run = world.child(task, pipeline)
    inputs = {name: spec.get("default") for name, spec in pipeline.inputs.items()}
    inputs.update(_inputs(world, task), project_path=str(world.case.repo))
    context: dict[str, Any] = {"inputs": inputs, "steps": {}}
    renderer = StepRenderer(TemplateEngine())
    for name in ("guard", "admit"):
        step = next(step for step in pipeline.steps if step.id == name)
        assert step.mcp is not None
        args = renderer.render_mcp_arguments(step.mcp.arguments or {}, context, drop_none=True)
        with session_context_for_test(run.id):
            reply = cast(dict[str, Any], await world.tools.call(step.mcp.tool, args))
        assert reply.get("success"), reply
        context["steps"][name] = {"output": reply}
    for name in ("developer", "reviewer", "coordinator"):
        step = next(step for step in pipeline.steps if step.id == name)
        assert step.mcp is not None
        args = renderer.render_mcp_arguments(
            step.mcp.arguments or {},
            {**context, "invocation_id": step_invocation_id(execution_id(run), name)},
            drop_none=True,
        )
        assert args["task_id"] == task.id
        assert args["claim"] is False
        assert args["project_path"] == (
            str(world.case.repo)
            if name == "coordinator"
            else context["steps"]["admit"]["output"]["project_path"]
        )
        context["steps"][name] = {"output": {"child_session_id": name, "pane_ref": name}}
