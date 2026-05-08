"""Tool proxy service."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from gobby.mcp_proxy.manager import MCPClientManager

from .argument_validation import (
    check_arguments,
    classify_error,
    is_argument_error,
    prepare_arguments,
)
from .resource_operations import (
    read_resource as read_resource_impl,
)
from .result_handling import (
    apply_before_tool_enforcement,
    build_before_tool_event,
)
from .server_resolution import (
    find_tool_server as find_tool_server_impl,
)
from .server_resolution import (
    get_server_suggestion,
    is_proxy_namespace,
    resolve_server_for_tool,
    resolve_server_name,
)
from .server_resolution import (
    list_servers as list_servers_impl,
)
from .session_context import (
    get_effective_session_id,
    get_requested_session_id,
    record_discovery_state,
    resolve_hook_manager,
    resolve_platform_session_id,
    resolve_tool_event_context,
)
from .tool_execution import (
    call_tool as call_tool_impl,
)
from .tool_execution import (
    call_tool_by_name as call_tool_by_name_impl,
)
from .tool_execution import (
    get_tool_schema as get_tool_schema_impl,
)
from .tool_execution import (
    list_tools as list_tools_impl,
)
from .tool_proxy_constants import PROXY_NAMESPACE, SERVER_SUGGESTIONS
from .tool_proxy_utils import safe_truncate

if TYPE_CHECKING:
    from gobby.hooks.hook_manager import HookManager
    from gobby.mcp_proxy.services.fallback import ToolFallbackResolver
    from gobby.mcp_proxy.tools.internal import InternalRegistryManager
    from gobby.storage.sessions import SessionManager


class ToolProxyService:
    """Service for proxying tool calls and resource reads to underlying MCP servers."""

    _PROXY_NAMESPACE = PROXY_NAMESPACE
    _SERVER_SUGGESTIONS = SERVER_SUGGESTIONS

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

    @property
    def session_manager(self) -> "SessionManager | None":
        """Expose the MCP manager session manager for shared context helpers."""
        return getattr(self._mcp_manager, "session_manager", None)

    def _resolve_server_name(self, server_name: str) -> str:
        """Auto-redirect known server name aliases to the correct server."""
        return resolve_server_name(self, server_name)

    def _get_server_suggestion(self, server_name: str) -> str | None:
        """Get a suggestion for a possibly misspelled server name."""
        return get_server_suggestion(self, server_name)

    def _is_proxy_namespace(self, server_name: str) -> bool:
        """Check if the server name is the proxy namespace rather than a real server."""
        return is_proxy_namespace(self, server_name)

    def _resolve_server_for_tool(self, tool_name: str) -> str | None:
        """Resolve the actual server name for a tool when given the proxy namespace."""
        return resolve_server_for_tool(self, tool_name)

    def _check_arguments(
        self,
        arguments: dict[str, Any],
        schema: dict[str, Any],
    ) -> list[str]:
        """Validate arguments against JSON schema."""
        return check_arguments(arguments, schema)

    def _is_argument_error(self, error_message: str) -> bool:
        """Detect if error message suggests invalid arguments."""
        return is_argument_error(error_message)

    def _classify_error(self, error_message: str, exception: Exception) -> str:
        """Classify an error into a structured error code."""
        return classify_error(self, error_message, exception)

    @staticmethod
    def _get_requested_session_id(session_id: str | None) -> str | None:
        """Return the explicit session reference or the current session context UUID."""
        return get_requested_session_id(session_id)

    def _get_effective_session_id(self, session_id: str | None) -> str | None:
        """Return the resolved platform session UUID for a session reference."""
        return get_effective_session_id(self, session_id)

    def _resolve_hook_manager(self) -> "HookManager | None":
        """Resolve HookManager lazily to avoid startup-order cycles."""
        return resolve_hook_manager(self)

    def _resolve_platform_session_id(self, session_id: str | None) -> str | None:
        """Resolve the best available session reference to a platform session UUID."""
        return resolve_platform_session_id(self, session_id)

    def _resolve_tool_event_context(
        self,
        effective_session_id: str,
    ) -> tuple[
        "HookManager | None", Any | None, Any | None, Any, dict[str, Any], str | None, str | None
    ]:
        """Resolve shared session metadata for direct tool lifecycle events."""
        return resolve_tool_event_context(self, effective_session_id)

    def _record_discovery_state(
        self,
        session_id: str | None,
        *,
        servers_listed: bool = False,
        listed_server: str | None = None,
    ) -> None:
        """Persist discovery state directly for proxy-executed discovery calls."""
        record_discovery_state(
            self,
            session_id,
            servers_listed=servers_listed,
            listed_server=listed_server,
        )

    def record_servers_listed(self, session_id: str | None = None) -> None:
        """Record a successful list_mcp_servers call for a session."""
        self._record_discovery_state(session_id, servers_listed=True)

    def record_listed_server(self, server_name: str, session_id: str | None = None) -> None:
        """Record a successful list_tools call for a specific server."""
        self._record_discovery_state(session_id, listed_server=server_name)

    def _prepare_arguments(
        self,
        arguments: dict[str, Any] | str | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Normalize tool arguments to a dict or return a structured error response."""
        return prepare_arguments(arguments)

    def _build_before_tool_event(
        self,
        effective_session_id: str,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        """Build the before_tool event used for direct MCP execution."""
        return build_before_tool_event(
            self, effective_session_id, server_name, tool_name, arguments
        )

    async def _apply_before_tool_enforcement(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        session_id: str | None,
    ) -> tuple[str, str, dict[str, Any], dict[str, Any] | None]:
        """Run workflow before_tool evaluation for direct MCP tool execution."""
        return await apply_before_tool_enforcement(
            self,
            server_name,
            tool_name,
            arguments,
            session_id,
        )

    async def list_tools(
        self,
        server_name: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """List tools for a specific server with progressive discovery format."""
        return await list_tools_impl(self, server_name, session_id)

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: str | dict[str, Any] | None = None,
        session_id: str | None = None,
        strip_unknown: bool = False,
        enforce_workflow: bool = True,
    ) -> Any:
        """Execute a tool with optional pre-validation."""
        return await call_tool_impl(
            self,
            server_name,
            tool_name,
            arguments,
            session_id,
            strip_unknown,
            enforce_workflow,
        )

    async def read_resource(self, server_name: str, uri: str) -> Any:
        """Read a resource."""
        return await read_resource_impl(self, server_name, uri)

    async def get_tool_schema(
        self,
        server_name: str,
        tool_name: str,
        session_id: str | None = None,
        record_discovery: bool = True,
    ) -> dict[str, Any]:
        """Get full schema for a specific tool."""
        return await get_tool_schema_impl(
            self,
            server_name,
            tool_name,
            session_id=session_id,
            record_discovery=record_discovery,
        )

    def find_tool_server(self, tool_name: str) -> str | None:
        """Find which server owns a tool by searching all available servers."""
        return find_tool_server_impl(self, tool_name)

    async def list_servers(self, name_filter: str | None = None) -> dict[str, Any]:
        """List all available MCP servers (internal + external)."""
        return await list_servers_impl(self, name_filter)

    async def call_tool_by_name(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> Any:
        """Call a tool by name, automatically resolving the server."""
        return await call_tool_by_name_impl(self, tool_name, arguments, session_id)


__all__ = ["ToolProxyService", "safe_truncate"]
