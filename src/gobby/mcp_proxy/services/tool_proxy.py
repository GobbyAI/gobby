"""Tool proxy service."""

import logging
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPError, ToolProxyErrorCode

if TYPE_CHECKING:
    from gobby.mcp_proxy.services.fallback import ToolFallbackResolver
    from gobby.mcp_proxy.tools.internal import InternalRegistryManager

logger = logging.getLogger("gobby.mcp.server")


def safe_truncate(text: str | bytes | None, length: int = 100) -> str:
    """Safely truncate text to length by unicode code points."""
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if len(text) <= length:
        return text
    return text[:length] + "..."


class ToolProxyService:
    """Service for proxying tool calls and resource reads to underlying MCP servers."""

    # The MCP proxy namespace. Agents see tools prefixed "mcp__gobby__" and
    # naturally assume "gobby" is the server name, but internal servers use
    # names like "gobby-tasks", "gobby-sessions", etc.
    _PROXY_NAMESPACE = "gobby"

    def __init__(
        self,
        mcp_manager: MCPClientManager,
        internal_manager: "InternalRegistryManager | None" = None,
        fallback_resolver: "ToolFallbackResolver | None" = None,
        validate_arguments: bool = True,
        tool_filter: Any = None,
    ):
        self._mcp_manager = mcp_manager
        self._internal_manager = internal_manager
        self._fallback_resolver = fallback_resolver
        self._validate_arguments = validate_arguments
        self._tool_filter = tool_filter

    def _is_proxy_namespace(self, server_name: str) -> bool:
        """Check if the server name is the proxy namespace rather than a real server."""
        return server_name == self._PROXY_NAMESPACE

    def _resolve_server_for_tool(self, tool_name: str) -> str | None:
        """Resolve the actual server name for a tool when given the proxy namespace.

        Logs a warning so we can track how often agents use "gobby" instead of
        the real server name.

        Args:
            tool_name: The tool to look up.

        Returns:
            The real server name, or None if the tool isn't found anywhere.
        """
        resolved = self.find_tool_server(tool_name)
        if resolved:
            logger.warning(
                f"Auto-resolved server_name='gobby' → '{resolved}' for tool '{tool_name}'"
            )
        else:
            logger.warning(
                f"server_name='gobby' used but tool '{tool_name}' not found on any server"
            )
        return resolved

    def _check_arguments(
        self,
        arguments: dict[str, Any],
        schema: dict[str, Any],
    ) -> list[str]:
        """
        Validate arguments against JSON schema.

        Returns list of validation errors, empty if valid.
        """
        errors = []
        properties = schema.get("properties", {})
        required = schema.get("required", [])

        # Check for unknown parameters (likely typos like workflow_name vs name)
        for key in arguments:
            if key not in properties:
                # Find similar parameter names for better error message
                similar = [p for p in properties if p in key or key in p]
                if similar:
                    errors.append(f"Unknown parameter '{key}'. Did you mean '{similar[0]}'?")
                else:
                    valid_params = list(properties.keys())
                    errors.append(f"Unknown parameter '{key}'. Valid parameters: {valid_params}")

        # Check for missing required parameters
        for req in required:
            if req not in arguments:
                errors.append(f"Missing required parameter '{req}'")

        return errors

    def _is_argument_error(self, error_message: str) -> bool:
        """Detect if error message suggests invalid arguments.

        Used to determine whether to include tool schema in error response
        to help the caller self-correct.
        """
        indicators = [
            "parameter",
            "argument",
            "required",
            "missing",
            "invalid",
            "unknown",
            "expected",
            "type error",
            "validation",
            "schema",
            "property",
            "field",
            "400",
            "422",
            "-32602",  # JSON-RPC invalid params error code
        ]
        error_lower = error_message.lower()
        return any(indicator in error_lower for indicator in indicators)

    def _classify_error(self, error_message: str, exception: Exception) -> str:
        """Classify an error into a structured error code.

        Used to provide structured error codes that consumers can rely on
        instead of fragile string matching.

        Args:
            error_message: The error message string
            exception: The original exception

        Returns:
            ToolProxyErrorCode value as string
        """
        error_lower = error_message.lower()

        # Check for server not found/configured errors
        if "server" in error_lower:
            if "not found" in error_lower:
                return ToolProxyErrorCode.SERVER_NOT_FOUND.value
            if "not configured" in error_lower:
                return ToolProxyErrorCode.SERVER_NOT_CONFIGURED.value

        # Check for tool not found
        if "tool" in error_lower and "not found" in error_lower:
            return ToolProxyErrorCode.TOOL_NOT_FOUND.value

        # Check for argument/validation errors
        if self._is_argument_error(error_message):
            return ToolProxyErrorCode.INVALID_ARGUMENTS.value

        # Check for connection errors
        connection_indicators = ["connection", "timeout", "refused", "unreachable", "circuit"]
        if any(ind in error_lower for ind in connection_indicators):
            return ToolProxyErrorCode.CONNECTION_ERROR.value

        # Default to execution error
        return ToolProxyErrorCode.EXECUTION_ERROR.value

    async def list_tools(
        self,
        server_name: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        List tools for a specific server with progressive disclosure format.

        When session_id is provided and a workflow is active, tools are filtered
        based on the current phase's allowed_tools and blocked_tools settings.

        Args:
            server_name: Server name (e.g., "gobby-tasks", "context7")
            session_id: Optional session ID to apply workflow phase filtering

        Returns:
            Dict with tool metadata: {"success": true, "tools": [...], "tool_count": N}
        """
        # Handle proxy namespace: aggregate tools from all internal registries
        if self._is_proxy_namespace(server_name):
            logger.warning(
                "list_tools called with server_name='gobby' — aggregating all internal tools"
            )
            if self._internal_manager:
                brief_tools: list[dict[str, Any]] = []
                for reg in self._internal_manager.get_all_registries():
                    for tool in reg.list_tools():
                        name = (
                            tool.get("name", "unknown")
                            if isinstance(tool, dict)
                            else getattr(tool, "name", "unknown")
                        )
                        desc = (
                            tool.get("description", "")
                            if isinstance(tool, dict)
                            else getattr(tool, "description", "")
                        )
                        brief_tools.append({"name": name, "brief": safe_truncate(desc)})
                if self._tool_filter and session_id:
                    brief_tools = self._tool_filter.filter_tools(brief_tools, session_id)
                return {"success": True, "tools": brief_tools, "tool_count": len(brief_tools)}
            return {"success": True, "tools": [], "tool_count": 0}

        # Check internal servers first (gobby-tasks, gobby-memory, etc.)
        if self._internal_manager and self._internal_manager.is_internal(server_name):
            registry = self._internal_manager.get_registry(server_name)
            if registry:
                tools = registry.list_tools()
                if self._tool_filter and session_id:
                    tools = self._tool_filter.filter_tools(tools, session_id)
                return {"success": True, "tools": tools, "tool_count": len(tools)}
            return {
                "success": False,
                "tools": [],
                "error": f"Internal server '{server_name}' not found",
            }

        # Check external servers
        if self._mcp_manager.has_server(server_name):
            tools_map = await self._mcp_manager.list_tools(server_name)
            tools_list = tools_map.get(server_name, [])
            # Convert to lightweight format
            ext_brief_tools: list[dict[str, Any]] = []
            for tool in tools_list:
                if isinstance(tool, dict):
                    ext_brief_tools.append(
                        {
                            "name": tool.get("name", "unknown"),
                            "brief": safe_truncate(tool.get("description", "")),
                        }
                    )
                else:
                    ext_brief_tools.append(
                        {
                            "name": tool.name,
                            "brief": safe_truncate(tool.description),
                        }
                    )
            if self._tool_filter and session_id:
                ext_brief_tools = self._tool_filter.filter_tools(ext_brief_tools, session_id)
            return {"success": True, "tools": ext_brief_tools, "tool_count": len(ext_brief_tools)}

        return {
            "success": False,
            "tools": [],
            "error": f"Server '{server_name}' not found",
        }

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        session_id: str | None = None,
        call_context: dict[str, Any] | None = None,
        strip_unknown: bool = False,
    ) -> Any:
        """Execute a tool with optional pre-validation.

        Pre-validates arguments against the tool's schema before execution.
        On validation error, returns the schema in the error response so
        the caller can self-correct in one round-trip.

        On execution error, includes fallback_suggestions if a fallback resolver
        is configured.

        When session_id is provided and a workflow is active, checks that the
        tool is not blocked by the current workflow step's blocked_tools setting.

        Args:
            strip_unknown: When True, silently strip unknown parameters instead
                of returning a validation error.  Used by internal dispatch paths
                (rule engine mcp_call effects) where context parameters like
                ``prompt_text`` are injected broadly but not every tool declares
                them.  Missing-required and type errors still fail.

        """
        arguments = arguments or {}

        # Check tool filter before execution
        if self._tool_filter and session_id:
            allowed, reason = self._tool_filter.is_tool_allowed(tool_name, session_id)
            if not allowed:
                return {
                    "success": False,
                    "error": reason,
                    "error_code": ToolProxyErrorCode.TOOL_BLOCKED.value,
                    "server_name": server_name,
                    "tool_name": tool_name,
                }

        # Handle proxy namespace: auto-resolve to the real server
        if self._is_proxy_namespace(server_name):
            resolved = self._resolve_server_for_tool(tool_name)
            if resolved:
                return await self.call_tool(
                    resolved,
                    tool_name,
                    arguments,
                    session_id,
                    call_context=call_context,
                    strip_unknown=strip_unknown,
                )
            return {
                "success": False,
                "error": f"Tool '{tool_name}' not found on any server (server_name='gobby' is not a real server — use list_mcp_servers() to discover server names)",
                "error_code": ToolProxyErrorCode.SERVER_NOT_FOUND.value,
                "server_name": server_name,
                "tool_name": tool_name,
            }

        # Pre-validate arguments if enabled
        if self._validate_arguments and arguments:
            schema_result = await self.get_tool_schema(server_name, tool_name)
            if schema_result.get("success"):
                input_schema = schema_result.get("tool", {}).get("inputSchema", {})
                if input_schema:
                    if strip_unknown:
                        # Silently remove parameters not in the schema
                        properties = input_schema.get("properties", {})
                        unknown_keys = [k for k in arguments if k not in properties]
                        for k in unknown_keys:
                            del arguments[k]
                        # Still validate required params
                        required = input_schema.get("required", [])
                        missing = [r for r in required if r not in arguments]
                        if missing:
                            return {
                                "success": False,
                                "error": f"Missing required parameters: {missing}",
                                "schema": input_schema,
                                "server_name": server_name,
                                "tool_name": tool_name,
                            }
                    else:
                        validation_errors = self._check_arguments(arguments, input_schema)
                        if validation_errors:
                            return {
                                "success": False,
                                "error": f"Invalid arguments: {validation_errors}",
                                "hint": "Review the schema below and retry with correct parameters",
                                "schema": input_schema,
                                "server_name": server_name,
                                "tool_name": tool_name,
                            }

        try:
            # Check internal tools first
            if self._internal_manager and self._internal_manager.is_internal(server_name):
                registry = self._internal_manager.get_registry(server_name)
                if registry:
                    return await registry.call(tool_name, arguments, context=call_context)
                raise MCPError(f"Internal server '{server_name}' not found")

            # Use MCP manager for external servers
            return await self._mcp_manager.call_tool(server_name, tool_name, arguments)

        except Exception as e:
            error_message = str(e)
            logger.warning(f"Tool call failed: {server_name}/{tool_name}: {error_message}")

            # Build error response with fallback suggestions
            response: dict[str, Any] = {
                "success": False,
                "error": error_message,
                "error_code": self._classify_error(error_message, e),
                "server_name": server_name,
                "tool_name": tool_name,
            }

            # Enrich with schema if error looks like an argument validation error
            if self._is_argument_error(error_message):
                try:
                    schema_result = await self.get_tool_schema(server_name, tool_name)
                    if schema_result.get("success"):
                        input_schema = schema_result.get("tool", {}).get("inputSchema", {})
                        if input_schema:
                            response["hint"] = (
                                "This appears to be an argument error. "
                                "Schema provided for self-correction."
                            )
                            response["schema"] = input_schema
                except Exception as schema_error:
                    logger.debug(f"Could not fetch schema for error enrichment: {schema_error}")

            # Get fallback suggestions if resolver is available
            if self._fallback_resolver:
                try:
                    project_id = self._mcp_manager.project_id
                    if not project_id:
                        from gobby.utils.project_context import get_project_context

                        ctx = get_project_context()
                        project_id = ctx.get("id") if ctx else None
                    if project_id:
                        suggestions = await self._fallback_resolver.find_alternatives_for_error(
                            server_name=server_name,
                            tool_name=tool_name,
                            error_message=error_message,
                            project_id=project_id,
                        )
                        response["fallback_suggestions"] = suggestions
                    else:
                        response["fallback_suggestions"] = []
                except Exception as fallback_error:
                    logger.debug(f"Fallback resolver failed: {fallback_error}")
                    response["fallback_suggestions"] = []
            else:
                response["fallback_suggestions"] = []

            return response

    async def read_resource(self, server_name: str, uri: str) -> Any:
        """Read a resource."""
        return await self._mcp_manager.read_resource(server_name, uri)

    async def get_tool_schema(self, server_name: str, tool_name: str) -> dict[str, Any]:
        """Get full schema for a specific tool."""
        # Handle proxy namespace: auto-resolve to the real server
        if self._is_proxy_namespace(server_name):
            resolved = self._resolve_server_for_tool(tool_name)
            if resolved:
                return await self.get_tool_schema(resolved, tool_name)
            return {
                "success": False,
                "error": f"Tool '{tool_name}' not found on any server (server_name='gobby' is not a real server — use list_mcp_servers() to discover server names)",
                "error_code": ToolProxyErrorCode.SERVER_NOT_FOUND.value,
            }

        # Check internal tools first
        if self._internal_manager and self._internal_manager.is_internal(server_name):
            registry = self._internal_manager.get_registry(server_name)
            if registry:
                schema = registry.get_schema(tool_name)
                if schema:
                    return {"success": True, "tool": schema}
                return {
                    "success": False,
                    "error": f"Tool '{tool_name}' not found on '{server_name}'",
                }
            return {"success": False, "error": f"Internal server '{server_name}' not found"}

        if not self._mcp_manager.has_server(server_name):
            return {"success": False, "error": f"Server '{server_name}' not found"}

        # Use MCP manager for external servers
        try:
            return await self._mcp_manager.get_tool_input_schema(server_name, tool_name)
        except Exception as e:
            raise MCPError(f"Failed to get schema for {tool_name} on {server_name}: {e}") from e

    def find_tool_server(self, tool_name: str) -> str | None:
        """
        Find which server owns a tool by searching all available servers.

        Searches internal registries first (faster), then external server configs.

        Args:
            tool_name: Name of the tool to find

        Returns:
            Server name if found, None otherwise
        """
        # Search internal registries first (fast, in-memory lookup)
        if self._internal_manager:
            server = self._internal_manager.find_tool_server(tool_name)
            if server:
                return server

        # Search external server configs (cached tool metadata)
        for server_name, config in self._mcp_manager._configs.items():
            if config.tools:
                for tool in config.tools:
                    tool_name_in_config = (
                        tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", None)
                    )
                    if tool_name_in_config == tool_name:
                        return server_name

        return None

    async def call_tool_by_name(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        session_id: str | None = None,
        call_context: dict[str, Any] | None = None,
    ) -> Any:
        """
        Call a tool by name, automatically resolving the server.

        Searches all available servers to find which one owns the tool,
        then routes the call appropriately.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments
            session_id: Optional session ID for workflow tool restriction checks

        Returns:
            Tool execution result, or error dict if tool not found
        """
        server_name = self.find_tool_server(tool_name)

        if server_name is None:
            logger.warning(f"Tool '{tool_name}' not found on any server")
            return {
                "success": False,
                "error": f"Tool '{tool_name}' not found on any available server",
                "tool_name": tool_name,
            }

        logger.debug(f"Routing tool '{tool_name}' to server '{server_name}'")
        return await self.call_tool(
            server_name, tool_name, arguments, session_id, call_context=call_context
        )
