"""Tests for call_tool pre-validation of arguments.

These tests verify that the ToolProxyService:
1. Returns helpful error with schema when wrong parameter names are used
2. Returns helpful error with schema when required parameters are missing
3. Error response includes full tool schema for reference
4. Valid parameters pass through normally
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks.events import HookEventType, HookResponse, SessionSource
from gobby.mcp_proxy.services.tool_proxy import ToolProxyService
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.hooks import WorkflowHookHandler
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.sync_rules import get_bundled_rules_path, sync_bundled_rules

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_mcp_manager():
    """Create a mock MCP manager."""
    manager = MagicMock()
    manager.project_id = "test-project-id"
    manager.call_tool = AsyncMock()
    manager.get_tool_schema = AsyncMock()
    return manager


@pytest.fixture
def mock_internal_manager():
    """Create a mock internal registry manager."""
    manager = MagicMock()
    manager.is_internal.return_value = False
    return manager


@pytest.fixture
def temp_db(tmp_path):
    """Create a real workflow DB for integration-style rule tests."""
    db_path = tmp_path / "test_tool_proxy_validation.db"
    database = LocalDatabase(db_path)
    run_migrations(database)
    return database


@pytest.fixture
def tool_proxy(mock_mcp_manager, mock_internal_manager):
    """Create ToolProxyService with validation enabled."""
    return ToolProxyService(
        mcp_manager=mock_mcp_manager,
        internal_manager=mock_internal_manager,
        validate_arguments=True,
    )


@pytest.fixture
def tool_proxy_no_validation(mock_mcp_manager, mock_internal_manager):
    """Create ToolProxyService with validation disabled."""
    return ToolProxyService(
        mcp_manager=mock_mcp_manager,
        internal_manager=mock_internal_manager,
        validate_arguments=False,
    )


class TestCheckArguments:
    """Tests for the _check_arguments validation method."""

    def test_valid_arguments_returns_empty_list(self, tool_proxy) -> None:
        """Verify valid arguments return no errors."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
            },
            "required": ["name"],
        }
        arguments = {"name": "test", "count": 5}

        errors = tool_proxy._check_arguments(arguments, schema)

        assert errors == []

    def test_unknown_parameter_returns_error(self, tool_proxy) -> None:
        """Verify unknown parameter names are flagged."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": [],
        }
        arguments = {"workflow_name": "test"}  # Wrong: should be "name"

        errors = tool_proxy._check_arguments(arguments, schema)

        assert len(errors) == 1
        assert "Unknown parameter 'workflow_name'" in errors[0]
        assert "name" in errors[0]  # Should suggest similar param

    def test_unknown_parameter_lists_valid_parameters(self, tool_proxy) -> None:
        """Verify error lists valid parameters when no similar match."""
        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": [],
        }
        arguments = {"xyz": "test"}  # No similar match

        errors = tool_proxy._check_arguments(arguments, schema)

        assert len(errors) == 1
        assert "Unknown parameter 'xyz'" in errors[0]
        assert "Valid parameters:" in errors[0]
        assert "title" in errors[0]

    def test_missing_required_parameter_returns_error(self, tool_proxy) -> None:
        """Verify missing required parameters are flagged."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["name", "session_id"],
        }
        arguments = {"name": "test"}  # Missing session_id

        errors = tool_proxy._check_arguments(arguments, schema)

        assert len(errors) == 1
        assert "Missing required parameter 'session_id'" in errors[0]

    def test_multiple_errors_returned(self, tool_proxy) -> None:
        """Verify multiple errors are returned together."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "session_id": {"type": "string"},
            },
            "required": ["name", "session_id"],
        }
        arguments = {"wrong_param": "test"}  # Missing both + unknown param

        errors = tool_proxy._check_arguments(arguments, schema)

        assert len(errors) == 3  # 1 unknown + 2 missing
        error_text = " ".join(errors)
        assert "Unknown parameter 'wrong_param'" in error_text
        assert "Missing required parameter 'name'" in error_text
        assert "Missing required parameter 'session_id'" in error_text

    def test_empty_arguments_with_no_required(self, tool_proxy) -> None:
        """Verify empty arguments pass if nothing is required."""
        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
            "required": [],
        }
        arguments = {}

        errors = tool_proxy._check_arguments(arguments, schema)

        assert errors == []

    def test_empty_schema_accepts_all(self, tool_proxy) -> None:
        """Verify empty schema accepts any arguments (no validation possible)."""
        schema = {}
        arguments = {"anything": "goes"}

        errors = tool_proxy._check_arguments(arguments, schema)

        # With no properties defined, we can't validate - but currently
        # the code would flag "anything" as unknown. This tests current behavior.
        # If empty properties means "accept all", this test should be updated.
        assert len(errors) == 1
        assert "Unknown parameter 'anything'" in errors[0]


class TestCallToolPreValidation:
    """Tests for call_tool pre-validation behavior."""

    @pytest.mark.asyncio
    async def test_returns_error_with_schema_for_invalid_args(self, tool_proxy, mock_mcp_manager):
        """Verify invalid arguments return error with schema included."""
        # Setup mock to return tool schema
        tool_proxy._mcp_manager = mock_mcp_manager
        mock_mcp_manager.get_tool_schema = AsyncMock(
            return_value={
                "name": "test_tool",
                "description": "A test tool",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "The name"},
                        "session_id": {"type": "string"},
                    },
                    "required": ["name"],
                },
            }
        )

        # Mock get_tool_schema on the service
        async def mock_get_schema(server, tool):
            return {
                "success": True,
                "tool": {
                    "name": tool,
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "session_id": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                },
            }

        tool_proxy.get_tool_schema = mock_get_schema

        # Call with wrong parameter name
        result = await tool_proxy.call_tool(
            server_name="test-server",
            tool_name="test_tool",
            arguments={"workflow_name": "test"},  # Wrong param name
        )

        assert result["success"] is False
        assert "Invalid arguments" in result["error"]
        assert "workflow_name" in result["error"]
        assert "schema" in result
        assert "hint" in result
        assert result["server_name"] == "test-server"
        assert result["tool_name"] == "test_tool"

    @pytest.mark.asyncio
    async def test_returns_error_with_schema_for_missing_required(
        self, tool_proxy, mock_mcp_manager
    ):
        """Verify missing required parameters return error with schema."""

        async def mock_get_schema(server, tool):
            return {
                "success": True,
                "tool": {
                    "name": tool,
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "session_id": {"type": "string"},
                        },
                        "required": ["name", "session_id"],
                    },
                },
            }

        tool_proxy.get_tool_schema = mock_get_schema

        # Call with missing required parameter
        result = await tool_proxy.call_tool(
            server_name="test-server",
            tool_name="test_tool",
            arguments={"name": "test"},  # Missing session_id
        )

        assert result["success"] is False
        assert "Invalid arguments" in result["error"]
        assert "session_id" in result["error"]
        assert "schema" in result

    @pytest.mark.asyncio
    async def test_valid_arguments_pass_through(self, tool_proxy, mock_mcp_manager):
        """Verify valid arguments pass through to execution."""

        async def mock_get_schema(server, tool):
            return {
                "success": True,
                "tool": {
                    "name": tool,
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                },
            }

        tool_proxy.get_tool_schema = mock_get_schema
        mock_mcp_manager.call_tool.return_value = {"success": True, "result": "done"}

        result = await tool_proxy.call_tool(
            server_name="test-server",
            tool_name="test_tool",
            arguments={"name": "test"},
        )

        assert result["success"] is True
        mock_mcp_manager.call_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_validation_when_disabled(self, tool_proxy_no_validation, mock_mcp_manager):
        """Verify no validation when validate_arguments is False."""
        mock_mcp_manager.call_tool.return_value = {"success": True}

        await tool_proxy_no_validation.call_tool(
            server_name="test-server",
            tool_name="test_tool",
            arguments={"wrong_param": "test"},  # Would fail validation
        )

        # Should pass through without validation
        mock_mcp_manager.call_tool.assert_called_once()
        assert mock_mcp_manager.call_tool.call_count == 1
        assert mock_mcp_manager.call_tool.call_args is not None

    @pytest.mark.asyncio
    async def test_no_validation_for_empty_arguments(self, tool_proxy, mock_mcp_manager):
        """Verify no validation is performed when arguments are empty."""
        mock_mcp_manager.call_tool.return_value = {"success": True}

        await tool_proxy.call_tool(
            server_name="test-server",
            tool_name="test_tool",
            arguments={},  # Empty args
        )

        # Should pass through - empty args don't trigger validation
        mock_mcp_manager.call_tool.assert_called_once()
        assert mock_mcp_manager.call_tool.call_count == 1
        assert mock_mcp_manager.call_tool.call_args is not None

    @pytest.mark.asyncio
    async def test_schema_fetch_failure_allows_execution(self, tool_proxy, mock_mcp_manager):
        """Verify tool execution proceeds when schema fetch fails."""

        async def mock_get_schema(server, tool):
            return {"success": False, "error": "Schema not found"}

        tool_proxy.get_tool_schema = mock_get_schema
        mock_mcp_manager.call_tool.return_value = {"success": True}

        await tool_proxy.call_tool(
            server_name="test-server",
            tool_name="test_tool",
            arguments={"some_param": "test"},
        )

        # Should still attempt execution when schema is unavailable
        mock_mcp_manager.call_tool.assert_called_once()
        assert mock_mcp_manager.call_tool.call_count == 1
        assert mock_mcp_manager.call_tool.call_args is not None


class TestCallToolInternalServer:
    """Tests for call_tool with internal servers (gobby-*)."""

    @pytest.mark.asyncio
    async def test_validates_internal_tool_arguments(self, tool_proxy, mock_internal_manager):
        """Verify internal tool arguments are validated."""
        # Setup internal server detection
        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(return_value={"success": True})
        mock_internal_manager.get_registry.return_value = mock_registry

        # Setup schema
        async def mock_get_schema(server, tool):
            return {
                "success": True,
                "tool": {
                    "name": tool,
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string"},
                        },
                        "required": ["task_id"],
                    },
                },
            }

        tool_proxy.get_tool_schema = mock_get_schema

        # Call with wrong parameter name
        result = await tool_proxy.call_tool(
            server_name="gobby-tasks",
            tool_name="get_task",
            arguments={"id": "gt-123"},  # Wrong: should be task_id
        )

        assert result["success"] is False
        assert "Unknown parameter 'id'" in result["error"]
        assert "task_id" in result["error"]  # Should suggest correct param

    @pytest.mark.asyncio
    async def test_valid_internal_tool_execution(self, tool_proxy, mock_internal_manager):
        """Verify valid internal tool calls execute successfully."""
        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(
            return_value={"success": True, "id": "gt-123", "title": "Test"}
        )
        mock_internal_manager.get_registry.return_value = mock_registry

        async def mock_get_schema(server, tool):
            return {
                "success": True,
                "tool": {
                    "name": tool,
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "task_id": {"type": "string"},
                        },
                        "required": ["task_id"],
                    },
                },
            }

        tool_proxy.get_tool_schema = mock_get_schema

        result = await tool_proxy.call_tool(
            server_name="gobby-tasks",
            tool_name="get_task",
            arguments={"task_id": "gt-123"},
        )

        assert "success" not in result
        assert result["id"] == "gt-123"
        mock_registry.call.assert_called_once_with("get_task", {"task_id": "gt-123"})


class TestIsArgumentError:
    """Tests for the _is_argument_error heuristic method."""

    def test_detects_missing_required_parameter(self, tool_proxy) -> None:
        """Verify detection of missing required parameter errors."""
        assert tool_proxy._is_argument_error("Missing required parameter 'name'") is True

    def test_detects_invalid_argument(self, tool_proxy) -> None:
        """Verify detection of invalid argument errors."""
        assert tool_proxy._is_argument_error("Invalid argument type for 'count'") is True

    def test_detects_unknown_parameter(self, tool_proxy) -> None:
        """Verify detection of unknown parameter errors."""
        assert tool_proxy._is_argument_error("Unknown parameter 'foo'") is True

    def test_detects_validation_error(self, tool_proxy) -> None:
        """Verify detection of validation errors."""
        assert tool_proxy._is_argument_error("Validation failed: expected string") is True

    def test_detects_http_400(self, tool_proxy) -> None:
        """Verify detection of HTTP 400 errors."""
        assert tool_proxy._is_argument_error("HTTP 400 Bad Request") is True

    def test_detects_http_422(self, tool_proxy) -> None:
        """Verify detection of HTTP 422 errors."""
        assert tool_proxy._is_argument_error("422 Unprocessable Entity") is True

    def test_detects_jsonrpc_invalid_params(self, tool_proxy) -> None:
        """Verify detection of JSON-RPC invalid params error code."""
        assert tool_proxy._is_argument_error("Error code -32602: Invalid params") is True

    def test_does_not_detect_connection_timeout(self, tool_proxy) -> None:
        """Verify connection timeout is NOT detected as argument error."""
        assert tool_proxy._is_argument_error("Connection timed out after 30s") is False

    def test_does_not_detect_server_not_found(self, tool_proxy) -> None:
        """Verify server not found is NOT detected as argument error."""
        assert tool_proxy._is_argument_error("Server 'foo' is not connected") is False

    def test_does_not_detect_internal_server_error(self, tool_proxy) -> None:
        """Verify generic 500 without validation keywords is NOT detected."""
        assert tool_proxy._is_argument_error("Internal server error") is False

    def test_case_insensitive(self, tool_proxy) -> None:
        """Verify detection is case insensitive."""
        assert tool_proxy._is_argument_error("MISSING REQUIRED FIELD") is True
        assert tool_proxy._is_argument_error("Invalid Argument") is True


class TestExecutionErrorSchemaEnrichment:
    """Tests for schema enrichment on execution errors."""

    @pytest.mark.asyncio
    async def test_schema_included_for_missing_parameter_error(self, tool_proxy, mock_mcp_manager):
        """Verify schema is included when execution fails with missing parameter error."""
        # Setup: MCP manager raises exception with argument-related message
        mock_mcp_manager.call_tool = AsyncMock(
            side_effect=Exception("Missing required parameter 'session_id'")
        )

        async def mock_get_schema(server, tool):
            return {
                "success": True,
                "tool": {
                    "name": tool,
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "session_id": {"type": "string"},
                        },
                        "required": ["name", "session_id"],
                    },
                },
            }

        tool_proxy.get_tool_schema = mock_get_schema

        result = await tool_proxy.call_tool(
            server_name="test-server",
            tool_name="test_tool",
            arguments={"name": "test"},  # Missing session_id
        )

        assert result["success"] is False
        assert "Missing required parameter" in result["error"]
        assert "schema" in result
        assert "hint" in result
        assert "session_id" in result["schema"]["required"]

    @pytest.mark.asyncio
    async def test_schema_included_for_invalid_argument_error(self, tool_proxy, mock_mcp_manager):
        """Verify schema is included when execution fails with invalid argument error."""
        mock_mcp_manager.call_tool = AsyncMock(
            side_effect=Exception("Invalid argument: 'count' must be an integer")
        )

        async def mock_get_schema(server, tool):
            return {
                "success": True,
                "tool": {
                    "name": tool,
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "count": {"type": "integer"},
                        },
                    },
                },
            }

        tool_proxy.get_tool_schema = mock_get_schema

        result = await tool_proxy.call_tool(
            server_name="test-server",
            tool_name="test_tool",
            arguments={"count": "not-a-number"},
        )

        assert result["success"] is False
        assert "schema" in result
        assert result["schema"]["properties"]["count"]["type"] == "integer"

    @pytest.mark.asyncio
    async def test_schema_not_included_for_connection_error(self, tool_proxy, mock_mcp_manager):
        """Verify schema is NOT included for connection/timeout errors."""
        mock_mcp_manager.call_tool = AsyncMock(
            side_effect=Exception("Connection timed out after 30s")
        )

        # Mock get_tool_schema for pre-validation to pass
        async def mock_get_schema(server, tool):
            return {
                "success": True,
                "tool": {
                    "name": tool,
                    "inputSchema": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    },
                },
            }

        tool_proxy.get_tool_schema = mock_get_schema

        result = await tool_proxy.call_tool(
            server_name="test-server",
            tool_name="test_tool",
            arguments={"name": "test"},
        )

        assert result["success"] is False
        assert "Connection timed out" in result["error"]
        assert "schema" not in result
        assert "hint" not in result

    @pytest.mark.asyncio
    async def test_schema_not_included_for_server_not_found(self, tool_proxy, mock_mcp_manager):
        """Verify schema is NOT included for server not found errors."""
        mock_mcp_manager.call_tool = AsyncMock(
            side_effect=Exception("Server 'foo' is not connected")
        )

        # Mock get_tool_schema for pre-validation to pass
        async def mock_get_schema(server, tool):
            return {
                "success": True,
                "tool": {
                    "name": tool,
                    "inputSchema": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                    },
                },
            }

        tool_proxy.get_tool_schema = mock_get_schema

        result = await tool_proxy.call_tool(
            server_name="foo",
            tool_name="test_tool",
            arguments={"name": "test"},
        )

        assert result["success"] is False
        assert "schema" not in result

    @pytest.mark.asyncio
    async def test_graceful_handling_when_schema_fetch_fails(self, tool_proxy, mock_mcp_manager):
        """Verify error enrichment handles schema fetch failure gracefully."""
        mock_mcp_manager.call_tool = AsyncMock(
            side_effect=Exception("Missing required parameter 'name'")
        )

        async def mock_get_schema_failure(server, tool):
            raise Exception("Schema fetch failed")

        tool_proxy.get_tool_schema = mock_get_schema_failure

        result = await tool_proxy.call_tool(
            server_name="test-server",
            tool_name="test_tool",
            arguments={},
        )

        # Should still return error response, just without schema
        assert result["success"] is False
        assert "Missing required parameter" in result["error"]
        assert "schema" not in result  # Schema fetch failed, so not included

    @pytest.mark.asyncio
    async def test_schema_not_included_when_schema_result_unsuccessful(
        self, tool_proxy, mock_mcp_manager
    ):
        """Verify schema is not included when get_tool_schema returns unsuccessful."""
        mock_mcp_manager.call_tool = AsyncMock(side_effect=Exception("Invalid argument 'foo'"))

        async def mock_get_schema_not_found(server, tool):
            return {"success": False, "error": "Tool not found"}

        tool_proxy.get_tool_schema = mock_get_schema_not_found

        result = await tool_proxy.call_tool(
            server_name="test-server",
            tool_name="test_tool",
            arguments={"foo": "bar"},
        )

        assert result["success"] is False
        assert "schema" not in result


class TestCallToolBlockedToolsEnforcement:
    """Tests for blocked_tools enforcement in call_tool.

    Verifies that workflow blocked_tools restrictions are enforced
    when session_id is provided and a workflow is active.
    """

    @pytest.fixture
    def mock_tool_filter(self):
        """Create a mock tool filter service."""
        filter_service = MagicMock()
        filter_service.is_tool_allowed.return_value = (True, None)
        return filter_service

    @pytest.fixture
    def tool_proxy_with_filter(self, mock_mcp_manager, mock_internal_manager, mock_tool_filter):
        """Create ToolProxyService with tool filter enabled."""
        return ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            tool_filter=mock_tool_filter,
            validate_arguments=False,  # Disable argument validation for these tests
        )

    @pytest.mark.asyncio
    async def test_blocked_tool_returns_error(
        self, tool_proxy_with_filter, mock_tool_filter, mock_mcp_manager
    ):
        """Verify blocked tool is rejected with TOOL_BLOCKED error code."""
        from gobby.mcp_proxy.models import ToolProxyErrorCode

        mock_tool_filter.is_tool_allowed.return_value = (
            False,
            "Tool 'Edit' is blocked in step 'review'",
        )

        result = await tool_proxy_with_filter.call_tool(
            server_name="test-server",
            tool_name="Edit",
            arguments={"file": "test.py"},
            session_id="session-123",
        )

        assert result["success"] is False
        assert result["error_code"] == ToolProxyErrorCode.TOOL_BLOCKED.value
        assert "blocked" in result["error"]
        assert result["tool_name"] == "Edit"
        mock_mcp_manager.call_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_allowed_tool_executes_normally(
        self, tool_proxy_with_filter, mock_tool_filter, mock_mcp_manager
    ):
        """Verify allowed tool executes when filter permits."""
        mock_tool_filter.is_tool_allowed.return_value = (True, None)
        mock_mcp_manager.call_tool = AsyncMock(return_value={"success": True, "data": "result"})

        await tool_proxy_with_filter.call_tool(
            server_name="test-server",
            tool_name="Read",
            arguments={"file": "test.py"},
            session_id="session-123",
        )

        mock_tool_filter.is_tool_allowed.assert_called_once_with("Read", "session-123")
        assert mock_tool_filter.is_tool_allowed.call_count == 1
        assert mock_tool_filter.is_tool_allowed.call_args is not None
        mock_mcp_manager.call_tool.assert_called_once()
        assert mock_mcp_manager.call_tool.call_count == 1
        assert mock_mcp_manager.call_tool.call_args is not None

    @pytest.mark.asyncio
    async def test_no_filter_check_without_session_id(
        self, tool_proxy_with_filter, mock_tool_filter, mock_mcp_manager
    ):
        """Verify filter is not checked when session_id is not provided."""
        mock_mcp_manager.call_tool = AsyncMock(return_value={"success": True})

        await tool_proxy_with_filter.call_tool(
            server_name="test-server",
            tool_name="Edit",
            arguments={"file": "test.py"},
            # No session_id
        )

        mock_tool_filter.is_tool_allowed.assert_not_called()
        assert mock_tool_filter.is_tool_allowed.call_count == 0
        assert not mock_tool_filter.is_tool_allowed.called
        mock_mcp_manager.call_tool.assert_called_once()
        assert mock_mcp_manager.call_tool.call_count == 1
        assert mock_mcp_manager.call_tool.call_args is not None

    @pytest.mark.asyncio
    async def test_no_filter_check_without_filter_service(
        self, mock_mcp_manager, mock_internal_manager
    ):
        """Verify call proceeds when no tool filter is configured."""
        proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            tool_filter=None,  # No filter
            validate_arguments=False,
        )
        mock_mcp_manager.call_tool = AsyncMock(return_value={"success": True})

        await proxy.call_tool(
            server_name="test-server",
            tool_name="Edit",
            arguments={"file": "test.py"},
            session_id="session-123",  # session_id provided but no filter
        )

        mock_mcp_manager.call_tool.assert_called_once()
        assert mock_mcp_manager.call_tool.call_count == 1
        assert mock_mcp_manager.call_tool.call_args is not None

    @pytest.mark.asyncio
    async def test_blocked_tool_not_allowed_reason_in_error(
        self, tool_proxy_with_filter, mock_tool_filter
    ):
        """Verify the exact reason from filter appears in error response."""
        mock_tool_filter.is_tool_allowed.return_value = (
            False,
            "Tool 'Write' is not in allowed list for step 'fetch_changes'",
        )

        result = await tool_proxy_with_filter.call_tool(
            server_name="gobby-internal",
            tool_name="Write",
            arguments={},
            session_id="session-456",
        )

        assert result["success"] is False
        assert "not in allowed list" in result["error"]
        assert "fetch_changes" in result["error"]


class TestWorkflowBeforeToolEnforcement:
    """Tests for workflow before_tool enforcement on direct MCP execution."""

    @pytest.fixture
    def mock_hook_manager(self):
        """Create a mock HookManager with workflow handler and session lookup."""
        workflow_handler = MagicMock()
        workflow_handler.evaluate.return_value = HookResponse(decision="allow")

        session = SimpleNamespace(
            source="codex",
            session_type="terminal",
            project_id="project-123",
            external_id="conv-123",
        )
        session_manager = MagicMock()
        session_manager.get.return_value = session
        session_manager.resolve_session_reference.side_effect = (
            lambda session_id, project_id=None: session_id
        )

        hook_manager = MagicMock()
        hook_manager._workflow_handler = workflow_handler
        hook_manager._session_manager = session_manager
        hook_manager._database = MagicMock()
        hook_manager.handle = MagicMock(return_value=HookResponse(decision="allow"))
        return hook_manager

    @pytest.fixture
    def tool_proxy_with_hooks(self, mock_mcp_manager, mock_internal_manager, mock_hook_manager):
        """Create ToolProxyService with workflow hook access enabled."""
        return ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            validate_arguments=False,
            hook_manager_resolver=lambda: mock_hook_manager,
        )

    @pytest.mark.asyncio
    async def test_workflow_block_prevents_execution(
        self, tool_proxy_with_hooks, mock_hook_manager, mock_internal_manager
    ):
        """Blocked before_tool response should prevent registry dispatch."""
        from gobby.mcp_proxy.models import ToolProxyErrorCode

        mock_hook_manager._workflow_handler.evaluate.return_value = HookResponse(
            decision="block",
            reason="reopen_task is blocked",
        )
        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(return_value={"success": True})
        mock_internal_manager.get_registry.return_value = mock_registry

        result = await tool_proxy_with_hooks.call_tool(
            server_name="gobby-tasks",
            tool_name="reopen_task",
            arguments={"task_id": "#123"},
            session_id="session-123",
        )

        assert result["success"] is False
        assert result["error_code"] == ToolProxyErrorCode.TOOL_BLOCKED.value
        assert "blocked" in result["error"]
        mock_registry.call.assert_not_called()

        event = mock_hook_manager._workflow_handler.evaluate.call_args.args[0]
        assert event.data["tool_name"] == "mcp__gobby__call_tool"
        assert event.data["tool_input"]["server_name"] == "gobby-tasks"
        assert event.data["tool_input"]["tool_name"] == "reopen_task"

    @pytest.mark.asyncio
    async def test_session_context_fallback_drives_workflow_enforcement(
        self, tool_proxy_with_hooks, mock_hook_manager, mock_internal_manager
    ):
        """SessionContext should be enough to enforce blocked MCP tools."""
        from gobby.mcp_proxy.models import ToolProxyErrorCode

        mock_hook_manager._workflow_handler.evaluate.return_value = HookResponse(
            decision="block",
            reason="MCP tool 'gobby-tasks-ops:submit_for_review' is blocked",
        )
        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(return_value={"success": True})
        mock_internal_manager.get_registry.return_value = mock_registry

        with session_context_for_test("session-from-context"):
            result = await tool_proxy_with_hooks.call_tool(
                server_name="gobby-tasks-ops",
                tool_name="submit_for_review",
                arguments={"task_id": "#123", "stage_name": "development"},
            )

        assert result["success"] is False
        assert result["error_code"] == ToolProxyErrorCode.TOOL_BLOCKED.value
        mock_registry.call.assert_not_called()

        event = mock_hook_manager._workflow_handler.evaluate.call_args.args[0]
        assert event.metadata["_platform_session_id"] == "session-from-context"

    @pytest.mark.asyncio
    async def test_call_tool_emits_pipeline_source_when_session_is_pipeline(
        self, tool_proxy_with_hooks, mock_hook_manager, mock_internal_manager
    ):
        """HookEvent emitted by call_tool must carry source='pipeline' for pipeline sessions.

        Regression for #12082: tool_proxy previously read the session off
        hook_manager._session_manager (service wrapper without .get), so the
        AttributeError was silently swallowed and source fell back to CODEX,
        causing require-schema-before-call to fire on pipeline MCP calls.
        """
        pipeline_session = SimpleNamespace(
            source="pipeline",
            session_type="terminal",
            project_id="project-123",
            external_id="pipeline-exec-123",
        )
        mock_hook_manager._session_manager.get.return_value = pipeline_session

        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(return_value={"success": True})
        mock_internal_manager.get_registry.return_value = mock_registry

        await tool_proxy_with_hooks.call_tool(
            server_name="gobby-tasks-ops",
            tool_name="start_expansion_run",
            arguments={"task_id": "#123"},
            session_id="pipeline-session-abc",
        )

        event = mock_hook_manager._workflow_handler.evaluate.call_args.args[0]
        assert event.source == SessionSource.PIPELINE

    @pytest.mark.asyncio
    async def test_call_tool_logs_warning_and_defaults_source_when_storage_raises(
        self, tool_proxy_with_hooks, mock_hook_manager, mock_internal_manager, caplog
    ):
        """Storage failures during source resolution must log WARNING and fall back to codex.

        Regression for #12082: the previous DEBUG-only log silently hid the
        AttributeError that caused source to default to CODEX; a WARNING ensures
        the regression surfaces on the first run.
        """
        import logging

        mock_hook_manager._session_manager.get.side_effect = RuntimeError(
            "simulated storage failure"
        )

        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(return_value={"success": True})
        mock_internal_manager.get_registry.return_value = mock_registry

        with caplog.at_level(logging.WARNING, logger="gobby.mcp_proxy.services.tool_proxy"):
            await tool_proxy_with_hooks.call_tool(
                server_name="gobby-tasks-ops",
                tool_name="start_expansion_run",
                arguments={"task_id": "#123"},
                session_id="pipeline-session-abc",
            )

        event = mock_hook_manager._workflow_handler.evaluate.call_args.args[0]
        assert event.source == SessionSource.CODEX

        matching = [
            rec
            for rec in caplog.records
            if rec.levelno == logging.WARNING and "Failed to load session" in rec.getMessage()
        ]
        assert matching, "Expected WARNING log for storage lookup failure"

    @pytest.mark.asyncio
    async def test_modified_input_is_applied_before_execution(
        self, tool_proxy_with_hooks, mock_hook_manager, mock_internal_manager
    ):
        """rewrite_input responses should update the actual dispatched arguments."""
        mock_hook_manager._workflow_handler.evaluate.return_value = HookResponse(
            decision="allow",
            modified_input={
                "server_name": "gobby-tasks",
                "tool_name": "close_task",
                "arguments": {"task_id": "#123", "skip_validation": False},
            },
        )
        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(return_value={"success": True})
        mock_internal_manager.get_registry.return_value = mock_registry

        result = await tool_proxy_with_hooks.call_tool(
            server_name="gobby-tasks",
            tool_name="close_task",
            arguments={"task_id": "#123", "skip_validation": True},
            session_id="session-123",
        )

        assert result == {}
        mock_registry.call.assert_called_once_with(
            "close_task",
            {"task_id": "#123", "skip_validation": False},
        )

    @pytest.mark.asyncio
    async def test_list_tools_records_listed_server_for_session(
        self, tool_proxy_with_hooks, mock_internal_manager
    ):
        """Successful discovery should persist listed_servers for the session."""
        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.list_tools.return_value = [{"name": "create_task"}]
        mock_internal_manager.get_registry.return_value = mock_registry

        with patch("gobby.workflows.state_manager.SessionVariableManager") as mock_svm_cls:
            result = await tool_proxy_with_hooks.list_tools(
                "gobby-tasks",
                session_id="session-123",
            )

        assert result["success"] is True
        mock_svm_cls.return_value.append_to_set_variable.assert_called_once_with(
            "session-123",
            "listed_servers",
            ["gobby-tasks"],
        )

    # NOTE: A previous test asserted that get_tool_schema directly mutated
    # `unlocked_tools` via SessionVariableManager. That direct write was
    # removed in favor of the `track-schema-lookup` rule firing off the
    # synthetic AFTER_TOOL event (see TestSyntheticCodexMcpAfterTool below
    # and tests/workflows/test_codex_skill_injection.py for end-to-end coverage).


class TestSyntheticCodexMcpAfterTool:
    """Tests for the internal Codex-terminal MCP AFTER_TOOL compatibility shim."""

    @pytest.fixture
    def mock_hook_manager(self):
        """Create a mock HookManager with workflow handler and session lookup."""
        workflow_handler = MagicMock()
        workflow_handler.evaluate.return_value = HookResponse(decision="allow")

        session = SimpleNamespace(
            source="codex",
            session_type="terminal",
            project_id="project-123",
            external_id="conv-123",
        )
        session_manager = MagicMock()
        session_manager.get.return_value = session
        session_manager.resolve_session_reference.side_effect = (
            lambda session_id, project_id=None: session_id
        )

        hook_manager = MagicMock()
        hook_manager._workflow_handler = workflow_handler
        hook_manager._session_manager = session_manager
        hook_manager._database = MagicMock()
        hook_manager.handle = MagicMock(return_value=HookResponse(decision="allow"))
        return hook_manager

    @pytest.fixture
    def tool_proxy_with_hooks(self, mock_mcp_manager, mock_internal_manager, mock_hook_manager):
        """Create ToolProxyService with workflow hook access enabled."""
        return ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            validate_arguments=False,
            hook_manager_resolver=lambda: mock_hook_manager,
        )

    @pytest.mark.asyncio
    async def test_internal_tool_success_emits_synthetic_after_tool(
        self, tool_proxy_with_hooks, mock_hook_manager, mock_internal_manager
    ) -> None:
        """Successful Codex-terminal MCP calls should emit one synthetic AFTER_TOOL event."""
        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(return_value={"id": "task-123", "ref": "#123"})
        mock_internal_manager.get_registry.return_value = mock_registry

        result = await tool_proxy_with_hooks.call_tool(
            server_name="gobby-tasks",
            tool_name="create_task",
            arguments={"title": "Test task"},
            session_id="session-123",
        )

        assert result == {"id": "task-123", "ref": "#123"}
        mock_hook_manager.handle.assert_called_once()

        event = mock_hook_manager.handle.call_args.args[0]
        assert event.event_type == HookEventType.AFTER_TOOL
        assert event.session_id == "conv-123"
        assert event.source == SessionSource.CODEX
        assert event.metadata["_platform_session_id"] == "session-123"
        assert event.metadata["_synthetic_codex_mcp_after_tool"] is True
        assert event.data["tool_name"] == "mcp__gobby__call_tool"
        assert event.data["tool_input"] == {
            "server_name": "gobby-tasks",
            "tool_name": "create_task",
            "arguments": {"title": "Test task"},
        }
        assert event.data["tool_output"] == {"result": {"id": "task-123", "ref": "#123"}}
        assert event.data["mcp_server"] == "gobby-tasks"
        assert event.data["mcp_tool"] == "create_task"

    @pytest.mark.asyncio
    async def test_internal_tool_success_resolves_numbered_session_ref(
        self, tool_proxy_with_hooks, mock_hook_manager, mock_internal_manager
    ) -> None:
        """Synthetic AFTER_TOOL events should resolve #N refs before session lookup."""
        resolved_session_id = "f4b198e5-7688-45d5-82f5-5606732c7a96"
        session = mock_hook_manager._session_manager.get.return_value
        mock_hook_manager._session_manager.resolve_session_reference.side_effect = (
            lambda session_id, project_id=None: (
                resolved_session_id if session_id == "#2985" else session_id
            )
        )
        mock_hook_manager._session_manager.get.side_effect = (
            lambda session_id: session if session_id == resolved_session_id else None
        )
        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(return_value={"id": "task-123", "ref": "#123"})
        mock_internal_manager.get_registry.return_value = mock_registry

        await tool_proxy_with_hooks.call_tool(
            server_name="gobby-tasks",
            tool_name="create_task",
            arguments={"title": "Test task"},
            session_id="#2985",
        )

        mock_hook_manager.handle.assert_called_once()
        event = mock_hook_manager.handle.call_args.args[0]
        assert event.metadata["_platform_session_id"] == resolved_session_id

    @pytest.mark.asyncio
    async def test_internal_tool_exception_marks_synthetic_after_tool_failure(
        self, tool_proxy_with_hooks, mock_hook_manager, mock_internal_manager
    ) -> None:
        """Execution exceptions should emit a failed synthetic AFTER_TOOL event."""
        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(side_effect=RuntimeError("boom"))
        mock_internal_manager.get_registry.return_value = mock_registry

        result = await tool_proxy_with_hooks.call_tool(
            server_name="gobby-tasks",
            tool_name="create_task",
            arguments={"title": "Test task"},
            session_id="session-123",
        )

        assert result["success"] is False
        assert result["error"] == "boom"
        mock_hook_manager.handle.assert_called_once()

        event = mock_hook_manager.handle.call_args.args[0]
        assert event.metadata["is_failure"] is True
        assert event.data["is_error"] is True
        assert event.data["tool_output"]["success"] is False
        assert event.data["tool_output"]["error"] == "boom"
        assert event.data["tool_output"]["result"]["error"] == "boom"

    @pytest.mark.asyncio
    async def test_non_codex_sessions_do_not_emit_synthetic_after_tool(
        self, tool_proxy_with_hooks, mock_hook_manager, mock_internal_manager
    ) -> None:
        """Other CLI sources should stay on their native AFTER_TOOL paths."""
        mock_hook_manager._session_manager.get.return_value.source = "claude"
        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(return_value={"id": "task-123"})
        mock_internal_manager.get_registry.return_value = mock_registry

        await tool_proxy_with_hooks.call_tool(
            server_name="gobby-tasks",
            tool_name="create_task",
            arguments={"title": "Test task"},
            session_id="session-123",
        )

        mock_hook_manager.handle.assert_not_called()
        assert mock_hook_manager.handle.call_count == 0
        assert not mock_hook_manager.handle.called

    @pytest.mark.asyncio
    async def test_web_chat_sessions_do_not_emit_synthetic_after_tool(
        self, tool_proxy_with_hooks, mock_hook_manager, mock_internal_manager
    ) -> None:
        """Codex app-server/web-chat sessions should keep their native completion events."""
        mock_hook_manager._session_manager.get.return_value.session_type = "web_chat"
        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(return_value={"id": "task-123"})
        mock_internal_manager.get_registry.return_value = mock_registry

        await tool_proxy_with_hooks.call_tool(
            server_name="gobby-tasks",
            tool_name="create_task",
            arguments={"title": "Test task"},
            session_id="session-123",
        )

        mock_hook_manager.handle.assert_not_called()
        assert mock_hook_manager.handle.call_count == 0
        assert not mock_hook_manager.handle.called

    @pytest.mark.asyncio
    async def test_internal_dispatch_paths_skip_synthetic_after_tool(
        self, tool_proxy_with_hooks, mock_hook_manager, mock_internal_manager
    ) -> None:
        """Internal rule-engine MCP calls should not emit compatibility AFTER_TOOL events."""
        mock_internal_manager.is_internal.return_value = True
        mock_registry = MagicMock()
        mock_registry.call = AsyncMock(return_value={"id": "task-123"})
        mock_internal_manager.get_registry.return_value = mock_registry

        await tool_proxy_with_hooks.call_tool(
            server_name="gobby-tasks",
            tool_name="create_task",
            arguments={"title": "Test task"},
            session_id="session-123",
            enforce_workflow=False,
        )

        mock_hook_manager.handle.assert_not_called()
        assert mock_hook_manager.handle.call_count == 0
        assert not mock_hook_manager.handle.called

    @pytest.mark.asyncio
    async def test_proxy_schema_after_tool_injects_task_creation(
        self, mock_mcp_manager, mock_internal_manager, temp_db
    ) -> None:
        """Codex terminal proxy schema shims should resolve #N refs before directives."""
        sync_bundled_rules(temp_db, get_bundled_rules_path())
        temp_db.execute(
            "UPDATE workflow_definitions SET source = 'installed' WHERE source = 'template'"
        )

        workflow_handler = WorkflowHookHandler(rule_engine=RuleEngine(db=temp_db))

        session = SimpleNamespace(
            source="codex",
            session_type="terminal",
            project_id="project-123",
            external_id="conv-123",
        )
        numbered_session_ref = "#2985"
        resolved_session_id = "f4b198e5-7688-45d5-82f5-5606732c7a96"
        session_manager = MagicMock()
        session_manager.get.side_effect = (
            lambda session_id: session if session_id == resolved_session_id else None
        )
        session_manager.resolve_session_reference.side_effect = (
            lambda session_id, project_id=None: (
                resolved_session_id if session_id == numbered_session_ref else session_id
            )
        )

        hook_manager = MagicMock()
        hook_manager._workflow_handler = workflow_handler
        hook_manager._session_manager = session_manager
        hook_manager._database = temp_db
        hook_manager.handle = workflow_handler.evaluate

        tool_proxy = ToolProxyService(
            mcp_manager=mock_mcp_manager,
            internal_manager=mock_internal_manager,
            validate_arguments=False,
            hook_manager_resolver=lambda: hook_manager,
        )

        await tool_proxy.emit_synthetic_proxy_after_tool(
            session_id=numbered_session_ref,
            tool_name="get_tool_schema",
            tool_input={"server_name": "gobby-tasks", "tool_name": "create_task"},
            result={"success": True, "tool": {"name": "create_task", "inputSchema": {}}},
        )

        variables = SessionVariableManager(temp_db).get_variables(resolved_session_id)
        assert "task-creation" not in variables.get("loaded_skills", [])


class TestStripUnknownParameters:
    """Tests for strip_unknown=True behavior in call_tool."""

    @pytest.mark.asyncio
    async def test_strip_unknown_removes_extra_params(self, tool_proxy, mock_mcp_manager):
        """Verify strip_unknown=True silently removes unknown parameters."""

        async def mock_get_schema(server, tool):
            return {
                "success": True,
                "tool": {
                    "name": tool,
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string"},
                            "limit": {"type": "integer"},
                        },
                        "required": ["session_id"],
                    },
                },
            }

        tool_proxy.get_tool_schema = mock_get_schema
        mock_mcp_manager.call_tool.return_value = {"success": True}

        result = await tool_proxy.call_tool(
            server_name="test-server",
            tool_name="test_tool",
            arguments={"session_id": "s1", "limit": 5, "prompt_text": "hello"},
            strip_unknown=True,
        )

        assert result["success"] is True
        # prompt_text should have been stripped before the call
        actual_args = mock_mcp_manager.call_tool.call_args[0][2]
        assert "prompt_text" not in actual_args
        assert actual_args["session_id"] == "s1"
        assert actual_args["limit"] == 5

    @pytest.mark.asyncio
    async def test_strip_unknown_still_fails_on_missing_required(
        self, tool_proxy, mock_mcp_manager
    ):
        """Verify strip_unknown=True still rejects missing required parameters."""

        async def mock_get_schema(server, tool):
            return {
                "success": True,
                "tool": {
                    "name": tool,
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string"},
                            "name": {"type": "string"},
                        },
                        "required": ["session_id", "name"],
                    },
                },
            }

        tool_proxy.get_tool_schema = mock_get_schema

        result = await tool_proxy.call_tool(
            server_name="test-server",
            tool_name="test_tool",
            arguments={"prompt_text": "hello"},  # Missing both required params
            strip_unknown=True,
        )

        assert result["success"] is False
        assert "Missing required parameters" in result["error"]

    @pytest.mark.asyncio
    async def test_strip_unknown_false_rejects_unknown_params(self, tool_proxy, mock_mcp_manager):
        """Verify strip_unknown=False (default) still rejects unknown parameters."""

        async def mock_get_schema(server, tool):
            return {
                "success": True,
                "tool": {
                    "name": tool,
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "session_id": {"type": "string"},
                        },
                        "required": [],
                    },
                },
            }

        tool_proxy.get_tool_schema = mock_get_schema

        result = await tool_proxy.call_tool(
            server_name="test-server",
            tool_name="test_tool",
            arguments={"session_id": "s1", "prompt_text": "hello"},
            strip_unknown=False,
        )

        assert result["success"] is False
        assert "Invalid arguments" in result["error"]
        assert "prompt_text" in result["error"]
