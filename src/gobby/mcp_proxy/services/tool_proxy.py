"""Tool proxy service."""

import asyncio
import logging
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.manager import MCPClientManager
from gobby.mcp_proxy.models import MCPError, ToolProxyErrorCode

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager
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

    # Common server name mismatches for auto-heal suggestions
    _SERVER_SUGGESTIONS = {
        # Workflows subsystems (rules, pipelines, variables all live under gobby-workflows)
        "gobby-pipelines": "gobby-workflows",
        "gobby-pipeline": "gobby-workflows",
        "gobby-rules": "gobby-workflows",
        "gobby-rule": "gobby-workflows",
        "gobby-variables": "gobby-workflows",
        "gobby-variable": "gobby-workflows",
        # Singular → plural
        "gobby-task": "gobby-tasks",
        "gobby-session": "gobby-sessions",
        "gobby-agent": "gobby-agents",
        "gobby-workflow": "gobby-workflows",
        "gobby-skill": "gobby-skills",
        "gobby-worktree": "gobby-worktrees",
        "gobby-clone": "gobby-clones",
        # Scheduler aliases → gobby-cron
        "gobby-scheduler": "gobby-cron",
        "gobby-schedule": "gobby-cron",
    }

    def __init__(
        self,
        mcp_manager: MCPClientManager,
        internal_manager: "InternalRegistryManager | None" = None,
        fallback_resolver: "ToolFallbackResolver | None" = None,
        validate_arguments: bool = True,
        tool_filter: Any = None,
        hook_manager_resolver: Callable[[], "HookManager | None"] | None = None,
    ):
        self._mcp_manager = mcp_manager
        self._internal_manager = internal_manager
        self._fallback_resolver = fallback_resolver
        self._validate_arguments = validate_arguments
        self._tool_filter = tool_filter
        self._hook_manager_resolver = hook_manager_resolver

    def _resolve_server_name(self, server_name: str) -> str:
        """Auto-redirect known server name aliases to the correct server."""
        return self._SERVER_SUGGESTIONS.get(server_name, server_name)

    def _get_server_suggestion(self, server_name: str) -> str | None:
        """Get a suggestion for a possibly misspelled server name."""
        return self._SERVER_SUGGESTIONS.get(server_name)

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

    @staticmethod
    def _get_effective_session_id(session_id: str | None) -> str | None:
        """Return the explicit session_id or the current session context UUID."""
        if session_id:
            return session_id

        from gobby.utils.session_context import get_session_context

        ctx = get_session_context()
        return ctx.session_id if ctx else None

    def _resolve_hook_manager(self) -> "HookManager | None":
        """Resolve HookManager lazily to avoid startup-order cycles."""
        if self._hook_manager_resolver is None:
            return None

        try:
            return self._hook_manager_resolver()
        except Exception as exc:
            logger.debug(f"Failed to resolve HookManager for tool enforcement: {exc}")
            return None

    def _prepare_arguments(
        self,
        arguments: dict[str, Any] | str | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Normalize tool arguments to a dict or return a structured error response."""
        arguments = arguments or {}

        if isinstance(arguments, str):
            from gobby.mcp_proxy._coerce_arguments import coerce_string_arguments

            parsed = coerce_string_arguments(arguments)
            if parsed is not None:
                arguments = parsed
            else:
                # Provide a more specific error when the JSON is valid but
                # not a dict (e.g. a list or scalar).
                import json as _json

                try:
                    val = _json.loads(arguments)
                    type_name = type(val).__name__
                except (ValueError, TypeError):
                    type_name = None

                if type_name:
                    error_msg = f"Invalid arguments: expected dict, got {type_name}"
                else:
                    error_msg = "Invalid arguments: expected dict, got string that isn't valid JSON"
                return None, {
                    "success": False,
                    "error": error_msg,
                    "error_code": ToolProxyErrorCode.INVALID_ARGUMENTS.value,
                }

        if isinstance(arguments, dict):
            normalized_arguments = dict(arguments)
            # Strip call_tool's own parameters that LLMs sometimes flatten into
            # the arguments dict instead of passing as separate parameters.
            for leaked_key in ("server_name", "tool_name"):
                normalized_arguments.pop(leaked_key, None)
            return normalized_arguments, None

        return None, {
            "success": False,
            "error": f"Invalid arguments: expected dict, got {type(arguments).__name__}",
            "error_code": ToolProxyErrorCode.INVALID_ARGUMENTS.value,
        }

    def _build_before_tool_event(
        self,
        effective_session_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        """Build the synthetic before_tool event used for direct MCP execution."""
        from gobby.hooks.events import HookEvent, HookEventType, SessionSource
        from gobby.utils.project_context import get_project_context

        project_ctx = get_project_context()
        cwd = project_ctx.get("project_path") if project_ctx else None
        project_id = project_ctx.get("id") if project_ctx else None
        source = SessionSource.CODEX
        metadata: dict[str, Any] = {"_platform_session_id": effective_session_id}

        hook_manager = self._resolve_hook_manager()
        session_manager = getattr(hook_manager, "_session_manager", None) if hook_manager else None
        if session_manager is not None:
            try:
                session = session_manager.get(effective_session_id)
            except Exception as exc:
                logger.debug(f"Failed to load session {effective_session_id} for tool event: {exc}")
            else:
                if session is not None:
                    session_source = getattr(session, "source", None)
                    if isinstance(session_source, str):
                        try:
                            source = SessionSource(session_source)
                        except ValueError:
                            logger.debug(
                                "Unknown session source %r for %s; defaulting to codex",
                                session_source,
                                effective_session_id,
                            )
                    project_id = project_id or getattr(session, "project_id", None)
                    external_id = getattr(session, "external_id", None)
                    if external_id:
                        metadata["external_id"] = external_id

        if cwd:
            metadata["project_path"] = cwd

        return HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=effective_session_id,
            source=source,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__call_tool",
                "tool_input": {
                    "server_name": server_name,
                    "tool_name": tool_name,
                    "arguments": deepcopy(arguments),
                },
            },
            metadata=metadata,
            cwd=cwd,
            project_id=project_id,
        )

    async def _apply_before_tool_enforcement(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        session_id: str | None,
    ) -> tuple[str, str, dict[str, Any], dict[str, Any] | None]:
        """Run workflow before_tool evaluation for direct MCP tool execution."""
        effective_session_id = self._get_effective_session_id(session_id)
        if not effective_session_id:
            return server_name, tool_name, arguments, None

        hook_manager = self._resolve_hook_manager()
        workflow_handler = (
            getattr(hook_manager, "_workflow_handler", None) if hook_manager else None
        )
        if workflow_handler is None:
            return server_name, tool_name, arguments, None

        event = self._build_before_tool_event(
            effective_session_id=effective_session_id,
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
        )
        response = await asyncio.to_thread(workflow_handler.evaluate, event)

        if response.decision != "allow":
            return (
                server_name,
                tool_name,
                arguments,
                {
                    "success": False,
                    "error": response.reason or "Tool call blocked by workflow rules.",
                    "error_code": ToolProxyErrorCode.TOOL_BLOCKED.value,
                    "server_name": server_name,
                    "tool_name": tool_name,
                },
            )

        modified_input = response.modified_input
        if not isinstance(modified_input, dict):
            return server_name, tool_name, arguments, None

        updated_server_name = modified_input.get("server_name", server_name)
        updated_tool_name = modified_input.get("tool_name", tool_name)
        raw_arguments = modified_input.get("arguments", arguments)
        updated_arguments, error = self._prepare_arguments(raw_arguments)
        if error is not None:
            error["server_name"] = str(updated_server_name)
            error["tool_name"] = str(updated_tool_name)
            return server_name, tool_name, arguments, error

        return str(updated_server_name), str(updated_tool_name), updated_arguments or {}, None

    async def list_tools(
        self,
        server_name: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        List tools for a specific server with progressive discovery format.

        When session_id is provided and a workflow is active, tools are filtered
        based on the current phase's allowed_tools and blocked_tools settings.

        Args:
            server_name: Server name (e.g., "gobby-tasks", "context7")
            session_id: Optional session ID to apply workflow phase filtering

        Returns:
            Dict with tool metadata: {"success": true, "tools": [...], "tool_count": N}
        """
        server_name = self._resolve_server_name(server_name)
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
            error_msg = f"Internal server '{server_name}' not found"
            suggestion = self._get_server_suggestion(server_name)
            if suggestion:
                error_msg += f". Did you mean '{suggestion}'?"
            return {
                "success": False,
                "tools": [],
                "error": error_msg,
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

        error_msg = f"Server '{server_name}' not found"
        suggestion = self._get_server_suggestion(server_name)
        if suggestion:
            error_msg += f". Did you mean '{suggestion}'?"
        return {
            "success": False,
            "tools": [],
            "error": error_msg,
        }

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        session_id: str | None = None,
        strip_unknown: bool = False,
        enforce_workflow: bool = True,
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
        server_name = self._resolve_server_name(server_name)
        prepared_arguments, error = self._prepare_arguments(arguments)
        if error is not None:
            return error
        arguments = prepared_arguments or {}

        # Handle proxy namespace: auto-resolve to the real server
        if self._is_proxy_namespace(server_name):
            resolved = self._resolve_server_for_tool(tool_name)
            if resolved:
                return await self.call_tool(
                    resolved,
                    tool_name,
                    arguments,
                    session_id,
                    strip_unknown=strip_unknown,
                    enforce_workflow=enforce_workflow,
                )
            return {
                "success": False,
                "error": f"Tool '{tool_name}' not found on any server (server_name='gobby' is not a real server — use list_mcp_servers() to discover server names)",
                "error_code": ToolProxyErrorCode.SERVER_NOT_FOUND.value,
                "server_name": server_name,
                "tool_name": tool_name,
            }

        if enforce_workflow:
            (
                server_name,
                tool_name,
                arguments,
                workflow_error,
            ) = await self._apply_before_tool_enforcement(
                server_name=server_name,
                tool_name=tool_name,
                arguments=arguments,
                session_id=session_id,
            )
            if workflow_error is not None:
                return workflow_error

        effective_session_id = self._get_effective_session_id(session_id)

        # Check tool filter before execution
        if self._tool_filter and effective_session_id:
            allowed, reason = self._tool_filter.is_tool_allowed(tool_name, effective_session_id)
            if not allowed:
                return {
                    "success": False,
                    "error": reason,
                    "error_code": ToolProxyErrorCode.TOOL_BLOCKED.value,
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
                    return await registry.call(tool_name, arguments)

                error_msg = f"Internal server '{server_name}' not found"
                suggestion = self._get_server_suggestion(server_name)
                if suggestion:
                    error_msg += f". Did you mean '{suggestion}'?"
                raise MCPError(error_msg)

            # Use MCP manager for external servers
            return await self._mcp_manager.call_tool(
                server_name, tool_name, arguments, session_id=effective_session_id
            )

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
        server_name = self._resolve_server_name(server_name)
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

            error_msg = f"Internal server '{server_name}' not found"
            suggestion = self._get_server_suggestion(server_name)
            if suggestion:
                error_msg += f". Did you mean '{suggestion}'?"
            return {"success": False, "error": error_msg}

        if not self._mcp_manager.has_server(server_name):
            error_msg = f"Server '{server_name}' not found"
            suggestion = self._get_server_suggestion(server_name)
            if suggestion:
                error_msg += f". Did you mean '{suggestion}'?"
            return {"success": False, "error": error_msg}

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

    async def list_servers(self, name_filter: str | None = None) -> dict[str, Any]:
        """List all available MCP servers (internal + external).

        Mirrors GobbyDaemonTools.list_mcp_servers() but lives on ToolProxyService
        so the hook dispatch code can reach it without the full server instance.

        Args:
            name_filter: Optional glob pattern to filter server names (e.g., "gobby-*").
        """
        import fnmatch

        server_list: list[dict[str, Any]] = []
        connected = 0
        if self._internal_manager:
            for reg in self._internal_manager.get_all_registries():
                server_list.append(
                    {"name": reg.name, "state": "connected", "transport": "internal"}
                )
                connected += 1
        for config in self._mcp_manager.server_configs:
            health = self._mcp_manager.health.get(config.name)
            state = health.state.value if health else "unknown"
            is_conn = config.name in self._mcp_manager.connections
            if is_conn:
                connected += 1
            entry: dict[str, Any] = {
                "name": config.name,
                "state": state,
                "transport": config.transport,
            }
            if not config.enabled:
                entry["enabled"] = False
            server_list.append(entry)

        if name_filter:
            server_list = [s for s in server_list if fnmatch.fnmatch(s["name"], name_filter)]
            connected = sum(1 for s in server_list if s.get("state") == "connected")

        return {
            "success": True,
            "servers": server_list,
            "total": len(server_list),
            "connected": connected,
        }

    async def call_tool_by_name(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        session_id: str | None = None,
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
        return await self.call_tool(server_name, tool_name, arguments, session_id)
