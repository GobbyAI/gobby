"""Tests for call_tool pre-validation of arguments.

These tests verify that the ToolProxyService:
1. Returns helpful error with schema when wrong parameter names are used
2. Returns helpful error with schema when required parameters are missing
3. Error response includes full tool schema for reference
4. Valid parameters pass through normally
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.mcp_proxy.services.tool_proxy import ToolProxyService

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
        mock_registry.call = AsyncMock(return_value={"id": "gt-123", "title": "Test"})
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

        assert result["id"] == "gt-123"
        mock_registry.call.assert_called_once_with("get_task", {"task_id": "gt-123"}, context=None)


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
        mock_mcp_manager.call_tool.assert_called_once()

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
        mock_mcp_manager.call_tool.assert_called_once()

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
