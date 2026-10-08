"""Designed guard refusals remain distinct from execution failures through logging."""

import logging
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.agents.runbook_seats import RunbookSeatRefusal
from gobby.config.logging import LoggingSettings
from gobby.mcp_proxy.services.argument_validation import check_arguments
from gobby.mcp_proxy.tools.agents_context import AgentsRegistryContext
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.runbook_seat_tools import register_runbook_seat_tools
from gobby.mcp_proxy.tools.workflows._pipeline_execution import _execute_pipeline_background
from gobby.telemetry.logging import _create_formatted_handlers, _handler_config
from gobby.workflows.definitions import MCPStepConfig, PipelineDefinition, PipelineStep
from gobby.workflows.pipeline_executor import PipelineExecutor
from gobby.workflows.pipeline_state import ExecutionStatus, PipelineStepError, StepStatus
from tests._timing import drain_asyncio_tasks

pytestmark = pytest.mark.unit

REFUSAL = (
    "worktree must resolve to exactly one candidate; candidates: lane-3-runbooks, lane-3-runbooks-2"
)


class GuardProxy:
    """Validate real guard arguments before invoking its registered callback."""

    def __init__(self, registry: InternalToolRegistry) -> None:
        self.registry = registry

    async def get_tool_schema(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.registry.get_schema("check_runbook_seats") or {}

    async def call_tool(
        self, server: str, tool: str, arguments: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        schema = self.registry.get_schema(tool)
        assert schema is not None
        errors = check_arguments(arguments, schema["inputSchema"])
        if errors:
            return {"success": False, "error": errors}
        return cast(dict[str, Any], await self.registry.call(tool, arguments))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["direct", "background", "nested", "detached"])
@pytest.mark.parametrize("refused", [True, False])
@pytest.mark.parametrize("reason", [REFUSAL, REFUSAL.replace("; ", ";\n")])
async def test_guard_logging_preserves_refusal_and_real_failure(
    mode: str,
    refused: bool,
    reason: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = InternalToolRegistry("gobby-agents")
    ctx = MagicMock(spec=AgentsRegistryContext)
    ctx.db = MagicMock()
    ctx.get_current_session_id.return_value = "caller"
    ctx.resolve_session_id.return_value = "caller"
    register_runbook_seat_tools(registry, ctx)
    proxy = GuardProxy(registry)
    guard = PipelineDefinition(
        name="guard-pipeline",
        inputs={"lane": {"type": "string", "default": "3"}},
        steps=[
            PipelineStep(
                id="guard",
                mcp=MCPStepConfig(
                    server="gobby-agents",
                    tool="check_runbook_seats",
                    arguments={
                        "requested": "researcher",
                        "catalogue": [],
                        "lane": "${{ inputs.lane }}",
                    },
                ),
            )
        ],
    )
    if not refused:
        assert guard.steps[0].mcp is not None
        assert guard.steps[0].mcp.arguments is not None
        guard.steps[0].mcp.arguments["lane"] = 3
    pipeline = guard
    loader = AsyncMock()
    if mode == "nested":
        loader.load_pipeline.return_value = guard
        pipeline = PipelineDefinition(
            name="parent-pipeline", steps=[PipelineStep(id="child", invoke_pipeline=guard.name)]
        )
    manager = MagicMock()
    execution = MagicMock()
    execution.id = "execution"
    execution.status = ExecutionStatus.RUNNING
    manager.create_execution.return_value = execution
    manager.get_execution.return_value = execution
    manager.update_execution_status.return_value = execution
    step = MagicMock()
    step.id = 1
    step.status = StepStatus.RUNNING
    manager.create_step_execution.return_value = step
    manager.update_step_execution.return_value = step
    manager.get_failed_steps.return_value = []
    manager.get_steps_for_execution.return_value = []
    executor = PipelineExecutor(
        db=MagicMock(),
        execution_manager=manager,
        llm_service=AsyncMock(),
        tool_proxy_getter=lambda: proxy,
        loader=loader,
    )
    errors_path = tmp_path / "errors.log"
    handlers = _create_formatted_handlers(
        _handler_config(LoggingSettings(dir=str(tmp_path))),
        logging.WARNING,
        logging.Formatter("%(levelname)s %(message)s"),
    )
    log = logging.getLogger("gobby")
    for handler in handlers:
        log.addHandler(handler)
    try:
        with (
            caplog.at_level(logging.WARNING, logger="gobby"),
            patch(
                "gobby.mcp_proxy.tools.runbook_seat_tools.resolve_crew_lane_inputs",
                side_effect=RunbookSeatRefusal(reason),
            ),
        ):
            if mode == "background":
                await _execute_pipeline_background(
                    executor, pipeline, {}, "project", execution.id, pipeline.name
                )
            elif mode == "detached":
                await executor.start_detached(pipeline=pipeline, inputs={}, project_id="project")
                task = next(iter(executor._detached_tasks))
                with pytest.raises(PipelineStepError):
                    await task
                await drain_asyncio_tasks()
                assert executor._detached_tasks == set()
                assert executor._detached_execution_ids == set()
            else:
                with pytest.raises(PipelineStepError):
                    await executor.execute(pipeline=pipeline, inputs={}, project_id="project")
    finally:
        for handler in handlers:
            log.removeHandler(handler)
            handler.close()
    records = [r for r in caplog.records if r.name.startswith("gobby.")]
    if refused:
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert REFUSAL in records[0].getMessage()
        assert "\n" not in records[0].getMessage()
        assert records[0].exc_info is None
        assert errors_path.read_text() == ""
        primary = (tmp_path / "automation.log").read_text()
        assert len(primary.splitlines()) == 1
        assert primary.startswith("WARNING ") and REFUSAL in primary
    else:
        assert any(r.levelno == logging.ERROR and r.exc_info for r in records)
        assert "Invalid type for parameter 'lane'" in errors_path.read_text()
        assert "Traceback" in errors_path.read_text()
    assert any(
        call.kwargs.get("status") == ExecutionStatus.FAILED
        for call in manager.update_execution_status.call_args_list
    )


def test_error_log_keeps_ordinary_warnings_and_tagged_errors(tmp_path: Path) -> None:
    handlers = _create_formatted_handlers(
        _handler_config(LoggingSettings(dir=str(tmp_path))),
        logging.WARNING,
        logging.Formatter("%(levelname)s %(message)s"),
    )
    try:
        for level, message in [
            (logging.WARNING, "ordinary warning"),
            (logging.ERROR, "real error"),
        ]:
            record = logging.LogRecord(
                "gobby.workflows.pipeline_executor", level, "", 0, message, (), None
            )
            if level == logging.ERROR:
                record.pipeline_guard_refused = True
            for handler in handlers:
                handler.handle(record)
    finally:
        for handler in handlers:
            handler.close()
    errors = (tmp_path / "errors.log").read_text()
    assert "WARNING ordinary warning" in errors
    assert "ERROR real error" in errors
