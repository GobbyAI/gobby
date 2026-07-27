"""Contract tests for the bundled wiki-research pipeline definition.

wiki-research is the single research entry point: ad-hoc runs come from
`gobby pipelines run wiki-research` or the dynamic `pipeline:wiki-research`
MCP tool, and standing queries are ordinary cron jobs pointing at this
pipeline. These tests lock in:

  - the YAML parses as a PipelineDefinition with expose_as_tool enabled,
  - typed inputs: question is the only required input; topic_slug,
    max_sources, max_items, create_tasks, provider, and model carry the
    documented defaults,
  - the reentry_check -> reentry_gate -> create_research_task ->
    spawn_researcher -> wait_researcher -> researcher_failed step chain,
  - the re-entrancy gate serializes runs per vault (running > 1 fails),
  - the research task embeds the question and hard budgets and routes to
    the wiki-researcher agent with provider/model passthrough,
  - the dynamic MCP tool registers as pipeline:wiki-research with question
    required in its input schema.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from gobby.mcp_proxy.tools.workflows._pipelines import _build_input_schema
from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.loader_cache import DiscoveredWorkflow

pytestmark = pytest.mark.unit

PIPELINE_PATH = Path("src/gobby/install/shared/workflows/pipelines/wiki-research.yaml")
PIPELINES_DIR = PIPELINE_PATH.parent

EXPECTED_STEP_ORDER = [
    "reentry_check",
    "reentry_gate",
    "create_research_task",
    "spawn_researcher",
    "wait_researcher",
    "researcher_failed",
]


@pytest.fixture(scope="module")
def pipeline() -> PipelineDefinition:
    with PIPELINE_PATH.open() as f:
        data = yaml.safe_load(f)
    return PipelineDefinition.model_validate(data)


def _step(pipeline: PipelineDefinition, step_id: str) -> PipelineStep:
    step = pipeline.get_step(step_id)
    assert step is not None, f"missing step {step_id}"
    return step


def test_task_steps_leave_session_context_to_proxy_wrapper() -> None:
    for path in PIPELINES_DIR.glob("*.yaml"):
        with path.open() as f:
            pipeline = PipelineDefinition.model_validate(yaml.safe_load(f))

        for step in pipeline.steps:
            if step.mcp is None or step.mcp.server != "gobby-tasks":
                continue
            assert "session_id" not in (step.mcp.arguments or {}), path


class TestWikiResearchDefinition:
    def test_parses_and_exposes_as_tool(self, pipeline: PipelineDefinition) -> None:
        assert pipeline.name == "wiki-research"
        assert pipeline.type == "pipeline"
        assert pipeline.expose_as_tool is True
        assert pipeline.enabled is True

    def test_question_is_the_only_required_input(self, pipeline: PipelineDefinition) -> None:
        question = pipeline.inputs["question"]
        assert question["type"] == "string"
        assert "default" not in question

        for name, input_def in pipeline.inputs.items():
            if name == "question":
                continue
            assert "default" in input_def, f"input {name} must carry a default"

    def test_budget_and_routing_defaults(self, pipeline: PipelineDefinition) -> None:
        defaults = {
            name: spec["default"] for name, spec in pipeline.inputs.items() if name != "question"
        }
        assert defaults == {
            "topic_slug": "",
            "max_sources": 12,
            "max_items": 8,
            "create_tasks": "false",
            "provider": "claude",
            "model": "sonnet",
        }

    def test_outputs_surface_task_run_and_status(self, pipeline: PipelineDefinition) -> None:
        assert "steps.create_research_task.output.id" in pipeline.outputs["research_task_id"]
        assert "steps.spawn_researcher.output.run_id" in pipeline.outputs["run_id"]
        assert "steps.wait_researcher.output.status" in pipeline.outputs["status"]


class TestWikiResearchSteps:
    def test_step_order(self, pipeline: PipelineDefinition) -> None:
        assert [step.id for step in pipeline.steps] == EXPECTED_STEP_ORDER

    def test_reentry_gate_serializes_runs(self, pipeline: PipelineDefinition) -> None:
        check = _step(pipeline, "reentry_check")
        assert check.mcp is not None
        assert check.mcp.server == "gobby-workflows"
        assert check.mcp.tool == "list_pipeline_executions"
        assert check.mcp.arguments == {"pipeline_name": "wiki-research", "status": "running"}

        gate = _step(pipeline, "reentry_gate")
        assert gate.condition is not None
        assert "len(reentry_check.output.executions) > 1" in gate.condition
        assert gate.mcp is not None
        assert gate.mcp.tool == "fail_pipeline"

    def test_research_task_embeds_question_and_budgets(self, pipeline: PipelineDefinition) -> None:
        step = _step(pipeline, "create_research_task")
        assert step.mcp is not None
        assert step.mcp.server == "gobby-tasks"
        assert step.mcp.tool == "create_task"
        args = step.mcp.arguments or {}

        assert args["category"] == "research"
        assert args["labels"] == ["wiki-research"]
        assert "session_id" not in args

        description = args["description"]
        for reference in (
            "${{ inputs.question }}",
            "${{ inputs.topic_slug }}",
            "${{ inputs.max_sources }}",
            "${{ inputs.max_items }}",
            "${{ inputs.create_tasks }}",
        ):
            assert reference in description, f"description must embed {reference}"

        validation_criteria = args["validation_criteria"]
        assert 'When create_tasks is "true"' in validation_criteria
        assert "every surviving finding has a linked task" in validation_criteria
        assert "every triaged-away item records its reason" in validation_criteria

    def test_spawn_routes_to_wiki_researcher(self, pipeline: PipelineDefinition) -> None:
        step = _step(pipeline, "spawn_researcher")
        assert step.mcp is not None
        assert step.mcp.server == "gobby-agents"
        assert step.mcp.tool == "spawn_agent"
        args = step.mcp.arguments or {}

        assert args["agent"] == "wiki-researcher"
        assert args["task_id"] == "${{ steps.create_research_task.output.id }}"
        assert args["provider"] == "${{ inputs.provider }}"
        assert args["model"] == "${{ inputs.model }}"
        assert args["timeout"] == 2700
        assert args["parent_session_id"] == "${{ session_id }}"

    def test_wait_and_failure_gate(self, pipeline: PipelineDefinition) -> None:
        wait = _step(pipeline, "wait_researcher")
        assert wait.wait is not None
        assert wait.wait["completion_id"] == "${{ steps.spawn_researcher.output.run_id }}"
        assert wait.wait["timeout"] == 3600

        failed = _step(pipeline, "researcher_failed")
        assert failed.condition is not None
        assert "steps.wait_researcher.output.status != 'success'" in failed.condition
        assert failed.mcp is not None
        assert failed.mcp.tool == "fail_pipeline"


class TestWikiResearchDynamicTool:
    def test_input_schema_requires_question_only(self, pipeline: PipelineDefinition) -> None:
        schema = _build_input_schema(pipeline)

        assert schema["required"] == ["question"]
        properties = schema["properties"]
        assert properties["question"]["type"] == "string"
        assert properties["max_sources"]["default"] == 12
        assert properties["max_items"]["default"] == 8
        assert properties["create_tasks"]["default"] == "false"
        assert properties["provider"]["default"] == "claude"
        assert properties["model"]["default"] == "sonnet"
        assert properties["topic_slug"]["default"] == ""
        # Meta-parameter added for every exposed pipeline.
        assert "continuation_prompt" in properties

    def test_registers_as_dynamic_mcp_tool(self, pipeline: PipelineDefinition) -> None:
        from gobby.mcp_proxy.tools.workflows import create_workflows_registry

        discovered = [
            DiscoveredWorkflow(
                name=pipeline.name,
                definition=pipeline,
                priority=100,
                is_project=False,
                path=PIPELINE_PATH,
            )
        ]
        loader = MagicMock()
        loader.discover_pipeline_workflows = AsyncMock(return_value=discovered)
        loader.discover_pipeline_workflows_sync.return_value = discovered
        loader.load_pipeline = AsyncMock()

        registry = create_workflows_registry(
            loader=loader,
            executor_getter=lambda: MagicMock(),
            execution_manager_getter=lambda: MagicMock(),
        )

        tool_names = [tool["name"] for tool in registry.list_tools()]
        assert "pipeline:wiki-research" in tool_names

        schema = registry.get_schema("pipeline:wiki-research")
        assert schema is not None
        assert schema["description"] == pipeline.description
        assert schema["inputSchema"]["required"] == ["question"]
        assert set(pipeline.inputs) <= set(schema["inputSchema"]["properties"])
