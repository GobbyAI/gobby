"""Pipeline MCP handler result-delivery coverage."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.services.result_offload import ToolResultOffloader
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.mcp_proxy.tools.internal import InternalRegistryManager, InternalToolRegistry
from gobby.workflows.definitions import MCPStepConfig, PipelineStep
from gobby.workflows.pipeline.handlers import execute_mcp_step

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_mcp_step_keeps_oversized_result_for_pipeline_dataflow() -> None:
    oversized = {"rows": [{"content": "x" * 12_000}]}
    registry = InternalToolRegistry("gobby-pipeline-test")
    registry.register(
        name="large_result",
        description="Return a large pipeline value.",
        input_schema={"type": "object", "properties": {}},
        func=lambda: oversized,
    )
    manager = InternalRegistryManager()
    manager.add_registry(registry)
    offloader = MagicMock(spec=ToolResultOffloader)
    offloader.maybe_offload = AsyncMock(return_value={"offloaded": True, "result_id": "unexpected"})
    proxy = ToolProxyService(
        mcp_manager=MagicMock(),
        internal_manager=manager,
        validate_arguments=False,
        result_offloader=cast(ToolResultOffloader, offloader),
    )
    step = PipelineStep(
        id="large_step",
        mcp=MCPStepConfig(server="gobby-pipeline-test", tool="large_result"),
    )

    result = await execute_mcp_step(
        step,
        {"inputs": {}, "steps": {}},
        lambda: proxy,
    )

    assert result is oversized
    assert len(result["rows"][0]["content"]) == 12_000
    offloader.maybe_offload.assert_not_awaited()
