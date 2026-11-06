"""Declared string inputs survive CLI parsing and pipeline argument rendering."""

from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.mcp_proxy.services.argument_validation import check_arguments
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.workflows.definitions import MCPStepConfig, PipelineDefinition, PipelineStep
from gobby.workflows.pipeline.renderer import StepRenderer
from gobby.workflows.pipeline_executor import PipelineExecutor
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


@pytest.mark.parametrize("condition, expected", [(True, "x"), (False, "y")])
def test_multiline_pure_expression_uses_normalized_parser(condition: bool, expected: str) -> None:
    renderer = StepRenderer(TemplateEngine())
    rendered = renderer.render_mcp_arguments(
        {"k": '${{ inputs.a\n  if inputs.b else "y" }}'},
        {"inputs": {"a": "x", "b": condition}, "_string_inputs": ["a"]},
    )

    assert rendered == {"k": expected}


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


@pytest.mark.asyncio
@pytest.mark.parametrize(("inputs", "expected"), [({}, 1800), ({"window": 900}, 900)])
async def test_integer_inputs_keep_native_values_in_mcp_arguments(
    mock_db: MagicMock,
    mock_execution_manager: MagicMock,
    mock_llm_service: MagicMock,
    inputs: dict[str, Any],
    expected: int,
) -> None:
    pipeline = PipelineDefinition(
        name="integer-input",
        inputs={"window": {"type": "integer", "default": 1800}},
        steps=[
            PipelineStep(
                id="echo",
                mcp=MCPStepConfig(
                    server="example", tool="echo", arguments={"window": "${{ inputs.window }}"}
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

    assert received == [{"window": expected}]
    assert isinstance(received[0]["window"], int)
