"""Tests for pipeline MCP tool registration and helpers.

Tests pipeline tool error paths, helper functions, and dynamic tool registration.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════════
# _require_pipeline
# ═══════════════════════════════════════════════════════════════════════


class TestRequirePipeline:
    """Tests for _require_pipeline helper."""

    def test_returns_error_for_non_pipeline(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import _require_pipeline

        mock_row = MagicMock()
        mock_row.workflow_type = "rule"
        mock_row.name = "my-rule"

        def_manager = MagicMock()
        with patch(
            "gobby.mcp_proxy.tools.workflows._pipelines._resolve_definition",
            return_value=mock_row,
        ):
            result = _require_pipeline(def_manager, name="my-rule")
            assert result is not None
            assert result["success"] is False
            assert "workflow, not a pipeline" in result["error"]

    def test_returns_none_for_pipeline(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import _require_pipeline

        mock_row = MagicMock()
        mock_row.workflow_type = "pipeline"

        def_manager = MagicMock()
        with patch(
            "gobby.mcp_proxy.tools.workflows._pipelines._resolve_definition",
            return_value=mock_row,
        ):
            result = _require_pipeline(def_manager, name="my-pipeline")
            assert result is None

    def test_returns_error_on_value_error(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import _require_pipeline

        def_manager = MagicMock()
        with patch(
            "gobby.mcp_proxy.tools.workflows._pipelines._resolve_definition",
            side_effect=ValueError("Not found"),
        ):
            result = _require_pipeline(def_manager, name="missing")
            assert result is not None
            assert result["success"] is False
            assert "Not found" in result["error"]


# ═══════════════════════════════════════════════════════════════════════
# _auto_subscribe_lineage
# ═══════════════════════════════════════════════════════════════════════


class TestAutoSubscribeLineage:
    """Tests for _auto_subscribe_lineage helper."""

    def test_registers_with_completion_registry(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import _auto_subscribe_lineage

        registry = MagicMock()
        _auto_subscribe_lineage(
            completion_registry=registry,
            completion_id="pe-1",
            session_id="sess-1",
            session_manager=None,
            continuation_prompt="Continue",
            db=None,
        )
        registry.register.assert_called_once()
        call_kwargs = registry.register.call_args
        assert "sess-1" in call_kwargs.kwargs["subscribers"]

    def test_handles_register_error(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import _auto_subscribe_lineage

        registry = MagicMock()
        registry.register.side_effect = RuntimeError("register failed")

        result = _auto_subscribe_lineage(
            completion_registry=registry,
            completion_id="pe-1",
            session_id="sess-1",
            session_manager=None,
            continuation_prompt=None,
            db=None,
        )
        assert result is None
        assert registry.register.call_count == 1


# ═══════════════════════════════════════════════════════════════════════
# _resolve_session_ref
# ═══════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════
# _build_input_schema
# ═══════════════════════════════════════════════════════════════════════


class TestBuildInputSchema:
    """Tests for _build_input_schema."""

    def test_dict_input_with_type_and_description(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import _build_input_schema

        pipeline = MagicMock()
        pipeline.inputs = {
            "target": {
                "type": "string",
                "description": "Target path",
                "default": "/tmp",
            }
        }

        schema = _build_input_schema(pipeline)
        assert schema["properties"]["target"]["type"] == "string"
        assert schema["properties"]["target"]["description"] == "Target path"
        assert schema["properties"]["target"]["default"] == "/tmp"
        # Has default, so not required
        assert "target" not in schema.get("required", [])

    def test_dict_input_without_default_is_required(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import _build_input_schema

        pipeline = MagicMock()
        pipeline.inputs = {"target": {"type": "string", "description": "Target path"}}

        schema = _build_input_schema(pipeline)
        assert "target" in schema["required"]

    def test_dict_input_without_type_defaults_to_string(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import _build_input_schema

        pipeline = MagicMock()
        pipeline.inputs = {"target": {"description": "Target"}}

        schema = _build_input_schema(pipeline)
        assert schema["properties"]["target"]["type"] == "string"

    def test_simple_value_input(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import _build_input_schema

        pipeline = MagicMock()
        pipeline.inputs = {"mode": "fast"}

        schema = _build_input_schema(pipeline)
        assert schema["properties"]["mode"]["type"] == "string"
        assert schema["properties"]["mode"]["default"] == "fast"

    def test_no_longer_includes_session_id(self) -> None:
        """session_id is read from SessionContext ContextVar, not from pipeline schema."""
        from gobby.mcp_proxy.tools.workflows._pipelines import _build_input_schema

        pipeline = MagicMock()
        pipeline.inputs = {}

        schema = _build_input_schema(pipeline)
        assert "session_id" not in schema["properties"]
        assert "session_id" not in schema.get("required", [])

    def test_includes_continuation_prompt(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import _build_input_schema

        pipeline = MagicMock()
        pipeline.inputs = {}

        schema = _build_input_schema(pipeline)
        assert "continuation_prompt" in schema["properties"]


# ═══════════════════════════════════════════════════════════════════════
# register_pipeline_tools
# ═══════════════════════════════════════════════════════════════════════


class TestRegisterPipelineTools:
    """Tests for register_pipeline_tools."""

    def test_registers_core_tools(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import register_pipeline_tools

        registry = InternalToolRegistry("test-registry")

        with patch("gobby.mcp_proxy.tools.workflows._pipelines._register_exposed_pipeline_tools"):
            register_pipeline_tools(registry)

        # Check core tools were registered
        assert registry.get_schema("list_pipelines") is not None
        assert registry.get_schema("get_pipeline") is not None
        assert registry.get_schema("run_pipeline") is not None
        assert registry.get_schema("approve_pipeline") is not None
        assert registry.get_schema("reject_pipeline") is not None
        assert registry.get_schema("get_pipeline_status") is not None
        assert registry.get_schema("wait_for_completion") is None

    def test_creates_def_manager_from_db(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import register_pipeline_tools

        registry = InternalToolRegistry("test-registry")
        db = MagicMock()

        with (
            patch("gobby.mcp_proxy.tools.workflows._pipelines._register_exposed_pipeline_tools"),
            patch(
                "gobby.mcp_proxy.tools.workflows._pipelines.LocalWorkflowDefinitionManager"
            ) as MockDM,
        ):
            register_pipeline_tools(registry, db=db)
            MockDM.assert_called_once_with(db)


# ═══════════════════════════════════════════════════════════════════════
# Tool function error paths
# ═══════════════════════════════════════════════════════════════════════


class TestToolFunctionErrors:
    """Tests for error paths in registered tool functions."""

    def test_wait_for_completion_not_registered(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import register_pipeline_tools

        registry = InternalToolRegistry("test")
        with patch("gobby.mcp_proxy.tools.workflows._pipelines._register_exposed_pipeline_tools"):
            register_pipeline_tools(registry)

        assert registry.get_schema("wait_for_completion") is None

    @pytest.mark.asyncio
    async def test_get_pipeline_status_no_em(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import register_pipeline_tools

        registry = InternalToolRegistry("test")
        with patch("gobby.mcp_proxy.tools.workflows._pipelines._register_exposed_pipeline_tools"):
            register_pipeline_tools(registry)

        result = await registry.call("get_pipeline_status", {"execution_id": "pe-1"})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_list_pipeline_executions_no_em(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import register_pipeline_tools

        registry = InternalToolRegistry("test")
        with patch("gobby.mcp_proxy.tools.workflows._pipelines._register_exposed_pipeline_tools"):
            register_pipeline_tools(registry)

        result = await registry.call("list_pipeline_executions", {})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_search_pipeline_executions_no_em(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import register_pipeline_tools

        registry = InternalToolRegistry("test")
        with patch("gobby.mcp_proxy.tools.workflows._pipelines._register_exposed_pipeline_tools"):
            register_pipeline_tools(registry)

        result = await registry.call("search_pipeline_executions", {"query": "test"})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_approve_pipeline_no_executor(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import register_pipeline_tools

        registry = InternalToolRegistry("test")
        with patch("gobby.mcp_proxy.tools.workflows._pipelines._register_exposed_pipeline_tools"):
            register_pipeline_tools(registry)

        result = await registry.call("approve_pipeline", {"token": "tok-1"})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_reject_pipeline_no_executor(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import register_pipeline_tools

        registry = InternalToolRegistry("test")
        with patch("gobby.mcp_proxy.tools.workflows._pipelines._register_exposed_pipeline_tools"):
            register_pipeline_tools(registry)

        result = await registry.call("reject_pipeline", {"token": "tok-1"})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_get_pipeline_no_loader(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import register_pipeline_tools

        registry = InternalToolRegistry("test")
        with patch("gobby.mcp_proxy.tools.workflows._pipelines._register_exposed_pipeline_tools"):
            register_pipeline_tools(registry, loader=None)

        result = await registry.call("get_pipeline", {"name": "test"})
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_create_pipeline_no_def_manager(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import register_pipeline_tools

        registry = InternalToolRegistry("test")
        with patch("gobby.mcp_proxy.tools.workflows._pipelines._register_exposed_pipeline_tools"):
            register_pipeline_tools(registry)

        result = await registry.call(
            "create_pipeline", {"yaml_content": "name: test\ntype: pipeline"}
        )
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_create_pipeline_invalid_yaml(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import register_pipeline_tools

        registry = InternalToolRegistry("test")
        db = MagicMock()
        loader = MagicMock()
        with patch("gobby.mcp_proxy.tools.workflows._pipelines._register_exposed_pipeline_tools"):
            register_pipeline_tools(registry, db=db, loader=loader)

        result = await registry.call("create_pipeline", {"yaml_content": ":\ninvalid: [yaml"})
        assert result["success"] is False
        assert "YAML" in result["error"]

    @pytest.mark.asyncio
    async def test_create_pipeline_not_pipeline_type(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import register_pipeline_tools

        registry = InternalToolRegistry("test")
        db = MagicMock()
        loader = MagicMock()
        with patch("gobby.mcp_proxy.tools.workflows._pipelines._register_exposed_pipeline_tools"):
            register_pipeline_tools(registry, db=db, loader=loader)

        result = await registry.call("create_pipeline", {"yaml_content": "name: test\ntype: rule"})
        assert result["success"] is False
        assert "type: pipeline" in result["error"]

    @pytest.mark.asyncio
    async def test_export_pipeline_no_def_manager(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import register_pipeline_tools

        registry = InternalToolRegistry("test")
        with patch("gobby.mcp_proxy.tools.workflows._pipelines._register_exposed_pipeline_tools"):
            register_pipeline_tools(registry)

        result = await registry.call("export_pipeline", {"name": "test"})
        assert result["success"] is False


# ═══════════════════════════════════════════════════════════════════════
# _register_exposed_pipeline_tools
# ═══════════════════════════════════════════════════════════════════════


class TestRegisterExposedPipelineTools:
    """Tests for _register_exposed_pipeline_tools."""

    def test_skips_when_no_loader(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import (
            _register_exposed_pipeline_tools,
        )

        registry = InternalToolRegistry("test")
        result = _register_exposed_pipeline_tools(registry, None, lambda: None)
        assert result is None
        assert registry._tools == {}

    def test_handles_discovery_error(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import (
            _register_exposed_pipeline_tools,
        )

        registry = InternalToolRegistry("test")
        loader = MagicMock()
        loader.discover_pipeline_workflows_sync.side_effect = RuntimeError("discover failed")

        result = _register_exposed_pipeline_tools(registry, loader, lambda: None)
        assert result is None
        assert registry._tools == {}

    def test_skips_non_exposed_pipelines(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import (
            _register_exposed_pipeline_tools,
        )

        registry = InternalToolRegistry("test")
        loader = MagicMock()

        mock_wf = MagicMock()
        mock_wf.definition.expose_as_tool = False
        loader.discover_pipeline_workflows_sync.return_value = [mock_wf]

        _register_exposed_pipeline_tools(registry, loader, lambda: None)
        # Non-exposed pipeline: tool should NOT be registered
        assert registry.get_schema("pipeline:test") is None

    def test_registers_exposed_pipeline_tool(self) -> None:
        from gobby.mcp_proxy.tools.workflows._pipelines import (
            _register_exposed_pipeline_tools,
        )

        registry = InternalToolRegistry("test")
        loader = MagicMock()

        mock_pipeline = MagicMock()
        mock_pipeline.name = "my-exposed"
        mock_pipeline.expose_as_tool = True
        mock_pipeline.description = "Run exposed pipeline"
        mock_pipeline.inputs = {}

        mock_wf = MagicMock()
        mock_wf.definition = mock_pipeline
        loader.discover_pipeline_workflows_sync.return_value = [mock_wf]

        _register_exposed_pipeline_tools(registry, loader, lambda: None)
        assert registry.get_schema("pipeline:my-exposed") is not None
