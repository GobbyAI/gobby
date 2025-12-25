"""
Internal tool registry for Gobby built-in tools.

Provides a registry system for internal tools that can be accessed via the
downstream proxy pattern (call_tool, list_tools, get_tool_schema) without
being registered directly on the FastMCP server.

This enables progressive disclosure for internal tools and reduces the
number of tools exposed on the main MCP server.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class InternalTool:
    """Represents an internal tool with its metadata and implementation."""

    name: str
    description: str
    input_schema: dict[str, Any]
    func: Callable[..., Any]


class InternalToolRegistry:
    """
    Registry for a domain of internal tools (e.g., gobby-tasks).

    Each registry represents a logical grouping of tools that can be
    discovered and called via the proxy pattern.
    """

    def __init__(self, name: str, description: str = ""):
        """
        Initialize a tool registry.

        Args:
            name: Server name (e.g., "gobby-tasks")
            description: Human-readable description of this tool domain
        """
        self.name = name
        self.description = description
        self._tools: dict[str, InternalTool] = {}

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        func: Callable[..., Any],
    ) -> None:
        """
        Register a tool with the registry.

        Args:
            name: Tool name
            description: Tool description (for progressive disclosure)
            input_schema: JSON Schema for the tool's input parameters
            func: The callable that implements the tool (sync or async)
        """
        self._tools[name] = InternalTool(
            name=name,
            description=description,
            input_schema=input_schema,
            func=func,
        )
        logger.debug(f"Registered internal tool '{name}' on '{self.name}'")

    async def call(self, name: str, args: dict[str, Any]) -> Any:
        """
        Call a tool by name with the given arguments.

        Args:
            name: Tool name
            args: Tool arguments

        Returns:
            Tool execution result

        Raises:
            ValueError: If tool not found
            Exception: If tool execution fails
        """
        tool = self._tools.get(name)
        if not tool:
            available = ", ".join(self._tools.keys())
            raise ValueError(
                f"Tool '{name}' not found on '{self.name}'. Available: {available}"
            )

        # Call the function (handle both sync and async)
        if asyncio.iscoroutinefunction(tool.func):
            return await tool.func(**args)
        return tool.func(**args)

    def list_tools(self) -> list[dict[str, str]]:
        """
        List all tools with lightweight metadata.

        Returns:
            List of {name, brief} dicts for progressive disclosure
        """
        return [
            {
                "name": tool.name,
                "brief": tool.description[:100] if tool.description else "No description",
            }
            for tool in self._tools.values()
        ]

    def get_schema(self, name: str) -> dict[str, Any] | None:
        """
        Get full schema for a specific tool.

        Args:
            name: Tool name

        Returns:
            Dict with name, description, and inputSchema, or None if not found
        """
        tool = self._tools.get(name)
        if not tool:
            return None

        return {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
        }

    def __len__(self) -> int:
        """Return number of registered tools."""
        return len(self._tools)


class InternalRegistryManager:
    """
    Manages multiple internal registries (gobby-tasks, gobby-hooks, etc.).

    Provides routing logic to dispatch calls to the appropriate registry
    based on server name prefix.
    """

    INTERNAL_PREFIX = "gobby-"

    def __init__(self) -> None:
        self._registries: dict[str, InternalToolRegistry] = {}

    def add_registry(self, registry: InternalToolRegistry) -> None:
        """
        Add a registry to the manager.

        Args:
            registry: The registry to add
        """
        self._registries[registry.name] = registry
        logger.info(
            f"Added internal registry '{registry.name}' with {len(registry)} tools"
        )

    def is_internal(self, server_name: str | None) -> bool:
        """
        Check if a server name refers to an internal registry.

        Args:
            server_name: Server name to check

        Returns:
            True if server_name starts with 'gobby-'
        """
        if server_name is None:
            return False
        return server_name.startswith(self.INTERNAL_PREFIX)

    def get_registry(self, server_name: str) -> InternalToolRegistry | None:
        """
        Get a registry by name.

        Args:
            server_name: Registry name (e.g., "gobby-tasks")

        Returns:
            The registry if found, None otherwise
        """
        return self._registries.get(server_name)

    def list_servers(self) -> list[dict[str, Any]]:
        """
        List all internal servers with metadata.

        Returns:
            List of server info dicts
        """
        return [
            {
                "name": registry.name,
                "description": registry.description,
                "tool_count": len(registry),
            }
            for registry in self._registries.values()
        ]

    def get_all_registries(self) -> list[InternalToolRegistry]:
        """
        Get all registered registries.

        Returns:
            List of all registries
        """
        return list(self._registries.values())

    def __len__(self) -> int:
        """Return number of registries."""
        return len(self._registries)
