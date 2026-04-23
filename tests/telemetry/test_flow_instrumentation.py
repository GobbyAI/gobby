"""
Integration tests for flow instrumentation.
Verifies that key flows (MCP, Pipelines, Hooks, Rules, Agents) create correct spans and attributes.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from gobby.telemetry.config import TelemetrySettings


@pytest.fixture
def tracer_provider(monkeypatch):
    """Fixture to provide a TracerProvider with an InMemorySpanExporter."""
    # Mock get_app_context to enable tracing
    mock_ctx = MagicMock()
    mock_ctx.config.telemetry = TelemetrySettings(traces_enabled=True)
    monkeypatch.setattr("gobby.app_context.get_app_context", lambda: mock_ctx)

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)

    # Use the mock provider for all tracers
    with patch("opentelemetry.trace.get_tracer_provider", return_value=provider):
        yield provider, exporter


@pytest.mark.asyncio
async def test_mcp_call_instrumentation(tracer_provider):
    """Test MCP Client Manager call_tool instrumentation."""
    _, exporter = tracer_provider

    from gobby.mcp_proxy.manager import MCPClientManager
    from gobby.mcp_proxy.models import MCPServerConfig

    # Mock dependencies
    mock_session = MagicMock()
    # call_tool must be async
    mock_session.call_tool = AsyncMock(return_value="tool-result")

    manager = MCPClientManager(server_configs=[])
    # Force mock session into connections
    mock_conn = MagicMock()
    mock_conn.session = mock_session
    manager._connections["test-server"] = mock_conn
    manager._configs["test-server"] = MCPServerConfig(
        name="test-server", transport="http", url="http://test", project_id="proj-1"
    )
    # Mock health entry
    mock_health = MagicMock()
    manager.health["test-server"] = mock_health

    # Mock ensure_connected to return our mock session
    with patch.object(manager, "ensure_connected", return_value=mock_session):
        result = await manager.call_tool("test-server", "test-tool", {"arg": "val"})

    assert result == "tool-result"

    spans = exporter.get_finished_spans()
    assert len(spans) >= 1
    mcp_span = next(s for s in spans if s.name == "mcp.call_tool")
    assert mcp_span.attributes["server_name"] == "test-server"
    assert mcp_span.attributes["tool_name"] == "test-tool"
    assert mcp_span.attributes["success"] is True
    assert "latency_ms" in mcp_span.attributes


@pytest.mark.asyncio
async def test_pipeline_instrumentation(tracer_provider):
    """Test Pipeline Executor instrumentation."""
    _, exporter = tracer_provider

    from gobby.workflows.definitions import PipelineDefinition, PipelineStep
    from gobby.workflows.pipeline_executor import PipelineExecutor

    # Mock dependencies
    mock_db = MagicMock()
    mock_exec_mgr = MagicMock()
    mock_llm = MagicMock()
    mock_approval = MagicMock()

    executor = PipelineExecutor(db=mock_db, execution_manager=mock_exec_mgr, llm_service=mock_llm)
    executor.approval_manager = mock_approval
    mock_approval.check_approval_gate = AsyncMock(return_value=None)

    pipeline = PipelineDefinition(
        name="test-pipeline", steps=[PipelineStep(id="step1", exec="echo hello")]
    )

    # We want to test _execute_step's span, so we mock only the bottom-level actions
    with patch(
        "gobby.workflows.pipeline_executor.execute_exec_step",
        side_effect=AsyncMock(return_value={"stdout": "hello", "exit_code": 0}),
    ):
        with patch.object(executor, "_emit_event", side_effect=AsyncMock(return_value=None)):
            with patch.object(
                executor, "_notify_completion", side_effect=AsyncMock(return_value=None)
            ):
                mock_exec_mgr.create_execution.return_value = MagicMock(id="exec-123")
                mock_exec_mgr.get_execution.return_value = MagicMock(
                    id="exec-123", status=MagicMock(value="running")
                )
                mock_exec_mgr.get_failed_steps.return_value = []
                mock_exec_mgr.update_execution_status.return_value = MagicMock(id="exec-123")
                mock_exec_mgr.create_step_execution.return_value = MagicMock(
                    id="step-exec-1", status="running", step_id="step1"
                )

                await executor.execute(pipeline, {}, "proj-1", execution_id="exec-123")

    spans = exporter.get_finished_spans()
    # Should have pipeline.execute and pipeline.step.step1
    pipeline_span = next(s for s in spans if s.name == "pipeline.execute")
    step_span = next(s for s in spans if s.name == "pipeline.step.step1")

    assert pipeline_span.attributes["pipeline_name"] == "test-pipeline"
    assert pipeline_span.attributes["status"] == "completed"
    assert pipeline_span.attributes["step_count"] == 1
    assert step_span.attributes["step_type"] == "exec"
    assert step_span.parent.span_id == pipeline_span.context.span_id


@pytest.mark.asyncio
async def test_hook_manager_instrumentation(tracer_provider):
    """Test Hook Manager instrumentation."""
    _, exporter = tracer_provider

    from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
    from gobby.hooks.hook_manager import HookManager

    manager = HookManager(daemon_host="localhost", daemon_port=1234)
    # Mock _handle_internal
    with patch.object(manager, "_handle_internal", return_value=HookResponse(decision="allow")):
        with patch.object(
            manager, "_get_cached_daemon_status", return_value=(True, "ready", "ready", None)
        ):
            event = HookEvent(
                event_type=HookEventType.SESSION_START,
                session_id="sess-123",
                source=SessionSource.CLAUDE,
                timestamp=datetime.now(UTC),
                data={},
            )
            await asyncio.to_thread(manager.handle, event)

    spans = exporter.get_finished_spans()
    hook_span = next(s for s in spans if s.name == "hook.handle")
    assert hook_span.attributes["event_type"] == "HookEventType.SESSION_START"
    assert hook_span.attributes["decision"] == "allow"


@pytest.mark.asyncio
async def test_rule_engine_instrumentation(tracer_provider):
    """Test Rule Engine instrumentation."""
    _, exporter = tracer_provider

    from gobby.hooks.events import HookEvent, HookEventType, SessionSource
    from gobby.workflows.engine.core import RuleEngine

    mock_db = MagicMock()
    # Mock ConfigStore.get to avoid DB fetch
    with patch("gobby.workflows.engine.core.ConfigStore") as mock_config_store_cls:
        mock_config_store = mock_config_store_cls.return_value
        mock_config_store.get.return_value = True  # enforcement_enabled

        engine = RuleEngine(mock_db)

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="sess-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "test-tool"},
        )

        # Mock internal methods
        with patch.object(engine, "_load_rules", return_value=[]):
            with patch.object(engine, "_load_session_overrides", return_value={}):
                await engine.evaluate(event, "sess-123", {})

    spans = exporter.get_finished_spans()
    rule_span = next(s for s in spans if s.name == "rules.evaluate")
    assert rule_span.attributes["event_type"] == "HookEventType.BEFORE_TOOL"
    assert rule_span.attributes["session_id"] == "sess-123"
    assert rule_span.attributes["final_decision"] == "allow"
