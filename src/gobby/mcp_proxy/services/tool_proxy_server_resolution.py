"""Server-name resolution helpers for the tool proxy service."""

import logging
from typing import Any, cast

logger = logging.getLogger("gobby.mcp.server")


def resolve_server_name(service: Any, server_name: str) -> str:
    """Auto-redirect known server name aliases to the correct server."""
    return cast("str", service._SERVER_SUGGESTIONS.get(server_name, server_name))


def get_server_suggestion(service: Any, server_name: str) -> str | None:
    """Get a suggestion for a possibly misspelled server name."""
    return cast("str | None", service._SERVER_SUGGESTIONS.get(server_name))


def is_proxy_namespace(service: Any, server_name: str) -> bool:
    """Check if the server name is the proxy namespace rather than a real server."""
    return cast("bool", server_name == service._PROXY_NAMESPACE)


def resolve_server_for_tool(service: Any, tool_name: str) -> str | None:
    """Resolve the actual server name for a tool when given the proxy namespace."""
    resolved = cast("str | None", service.find_tool_server(tool_name))
    if resolved:
        logger.warning(f"Auto-resolved server_name='gobby' → '{resolved}' for tool '{tool_name}'")
    else:
        logger.warning(f"server_name='gobby' used but tool '{tool_name}' not found on any server")
    return resolved
