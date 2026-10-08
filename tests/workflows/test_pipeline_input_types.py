"""Declared string inputs survive CLI parsing and pipeline argument rendering."""

from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner

from gobby.agents.runbook_seats import RunbookSeatRefusal
from gobby.cli.pipelines import pipelines
from gobby.mcp_proxy.services.argument_validation import check_arguments
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.runbook_seat_tools import register_runbook_seat_tools
from gobby.workflows.definitions import MCPStepConfig, PipelineDefinition, PipelineStep
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_executor import PipelineExecutor
from gobby.workflows.pipeline_state import ExecutionStatus
from gobby.workflows.templates import TemplateEngine

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", ["3", "003", "3.5", "true", "false", "null", "none", ""])
@pytest.mark.parametrize("expression", ["inputs.value", "inputs['value']", 'inputs["value"]'])
def test_declared_string_inputs_keep_their_text(value: str, expression: str) -> None:
    renderer = StepRenderer(TemplateEngine())
    step = PipelineStep(
        id="guard",
        mcp=MCPStepConfig(
            server="example",
            tool="guard",
            arguments={"value": "${{ " + expression + " }}", "nested": ["${{ inputs.value }}"]},
        ),
    )
    rendered = renderer.render_step(
        step, {"inputs": {"value": value}, "_string_inputs": frozenset({"value"})}
    )

    assert rendered.mcp.arguments == {"value": value, "nested": [value]}


def test_declared_optional_string_and_untyped_values_keep_existing_semantics() -> None:
    renderer = StepRenderer(TemplateEngine())
    arguments = {
        "optional": "${{ inputs.optional }}",
        "untyped": "${{ inputs.untyped }}",
        "output": "${{ steps.count.output }}",
    }
    context = {
        "inputs": {"optional": None, "untyped": "3"},
        "steps": {"count": {"output": "4"}},
        "_string_inputs": frozenset({"optional"}),
    }

    assert renderer.render_mcp_arguments(arguments, context, drop_none=True) == {
        "untyped": 3,
        "output": 4,
    }


class GuardProxy:
    """Use the real guard schema and callback, without launching a seat."""

    def __init__(self, registry: InternalToolRegistry) -> None:
        self.registry = registry
        self.calls: list[dict[str, Any]] = []

    async def call_tool(
        self, server: str, tool: str, arguments: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        assert (server, tool) == ("gobby-agents", "check_runbook_seats")
        schema = self.registry.get_schema(tool)
        assert schema is not None
        errors = check_arguments(arguments, schema["inputSchema"])
        if errors:
            return {"success": False, "error": str(errors)}
        self.calls.append(arguments)
        return cast(dict[str, Any], await self.registry.call(tool, arguments))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("default", "inputs", "expected"),
    [(3, {}, "3"), ("default", {"value": 3}, "3"), ("default", {"value": None}, None)],
)
async def test_string_defaults_and_native_overrides_are_normalized(
    mock_db: MagicMock,
    mock_execution_manager: MagicMock,
    mock_llm_service: MagicMock,
    default: str | int,
    inputs: dict[str, Any],
    expected: str | None,
) -> None:
    pipeline = PipelineDefinition(
        name="input-types",
        inputs={"value": {"type": "string", "default": default}},
        steps=[
            PipelineStep(
                id="echo",
                mcp=MCPStepConfig(
                    server="example", tool="echo", arguments={"value": "${{ inputs.value }}"}
                ),
            )
        ],
    )
    received: list[dict[str, Any]] = []

    async def echo(
        server: str, tool: str, arguments: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        received.append(arguments)
        return {"success": True}

    proxy = MagicMock()
    proxy.call_tool = AsyncMock(side_effect=echo)
    executor = PipelineExecutor(
        db=mock_db,
        execution_manager=mock_execution_manager,
        llm_service=mock_llm_service,
        template_engine=TemplateEngine(),
        tool_proxy_getter=lambda: proxy,
    )
    with patch("gobby.utils.project_context.get_project_context", return_value=None):
        await executor.execute(pipeline, inputs, "11111111-1111-4111-8111-111111111111")

    assert received == [{"value": expected} if expected is not None else {}]


def test_real_two_input_cli_command_reaches_the_lane_guard(
    mock_db: MagicMock, mock_execution_manager: MagicMock, mock_llm_service: MagicMock
) -> None:
    root = Path(__file__).resolve().parents[2]
    definition = yaml.safe_load((root / ".gobby/workflows/pipelines/crew-lane.yaml").read_text())
    # Keep the actual input declarations and guard; this test never executes a spawn step.
    definition["steps"] = definition["steps"][:1]
    pipeline = PipelineDefinition.model_validate(definition)
    registry = InternalToolRegistry("gobby-agents")
    caller = "11111111-1111-4111-8111-111111111111"
    ctx = MagicMock(spec=AgentsRegistryContext)
    ctx.db = mock_db
    ctx.get_current_session_id.return_value = caller
    ctx.resolve_session_id.return_value = caller
    register_runbook_seat_tools(registry, ctx)
    proxy = GuardProxy(registry)
    execution = cast(MagicMock, mock_execution_manager.create_execution.return_value)
    execution.pipeline_name = "crew-lane"

    def update_status(*, execution_id: str, status: ExecutionStatus, **kwargs: Any) -> MagicMock:
        execution.status = status
        execution.outputs_json = kwargs.get("outputs_json")
        return execution

    mock_execution_manager.update_execution_status.side_effect = update_status
    executor = PipelineExecutor(
        db=mock_db,
        execution_manager=mock_execution_manager,
        llm_service=mock_llm_service,
        template_engine=TemplateEngine(),
        tool_proxy_getter=lambda: proxy,
    )
    loader = MagicMock()
    loader.load_pipeline_sync.return_value = pipeline
    with (
        patch("gobby.cli.pipelines.get_workflow_loader", return_value=loader),
        patch("gobby.cli.pipelines._get_project_id", return_value=caller),
        patch("gobby.cli.pipelines._try_daemon_run", return_value=None),
        patch("gobby.cli.pipelines.get_pipeline_executor", return_value=executor),
        patch("gobby.utils.project_context.get_project_context", return_value=None),
        patch(
            "gobby.mcp_proxy.tools.runbook_seat_tools.resolve_crew_lane_inputs",
            side_effect=RunbookSeatRefusal("isolated lane guard reached; no seat launched"),
        ) as resolve,
    ):
        result = CliRunner().invoke(
            pipelines, ["run", "crew-lane", "-i", "seats=researcher", "-i", "lane=3", "--json"]
        )

    assert result.exit_code == 1  # The isolated guard deliberately refuses the launch.
    assert len(proxy.calls) == 1
    assert proxy.calls[0]["lane"] == "3"
    assert proxy.calls[0]["requested"] == "researcher"
    resolve.assert_called_once_with(
        mock_db,
        caller_session_id=caller,
        lane="3",
        workspace=None,
        report_to=None,
        worktree=None,
        lane_pane=None,
    )
