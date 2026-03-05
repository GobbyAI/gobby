"""
Internal tool registry for Gobby built-in tools.

Provides a registry system for internal tools that can be accessed via the
downstream proxy pattern (call_tool, list_tools, get_tool_schema) without
being registered directly on the FastMCP server.

This enables progressive discovery for internal tools and reduces the
number of tools exposed on the main MCP server.
"""

import inspect
import json
import logging
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Union, get_args, get_origin, get_type_hints

logger = logging.getLogger(__name__)


def _get_json_schema_type(annotation: Any) -> str:
    """
    Convert a Python type annotation to a JSON schema type string.

    Handles:
    - Basic types: int, bool, str, dict, list
    - Generic types: dict[str, Any], list[str], etc.
    - Union types: str | None, dict[str, Any] | None, etc.
    - typing.Union: Union[str, None], etc.

    Args:
        annotation: Python type annotation

    Returns:
        JSON schema type string ("string", "integer", "boolean", "object", "array")
    """
    if annotation is inspect.Parameter.empty:
        return "string"

    # Handle Union types (X | Y or Union[X, Y])
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        # Get non-None types from the union
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if args:
            # Use the first non-None type
            return _get_json_schema_type(args[0])
        return "string"

    # Handle generic types like dict[str, Any], list[str]
    if origin is dict:
        return "object"
    if origin is list:
        return "array"

    # Handle basic types
    if annotation is bool:
        return "boolean"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    if annotation is dict:
        return "object"
    if annotation is list:
        return "array"
    if annotation is str:
        return "string"

    # Default to string for unknown types
    return "string"


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
            description: Tool description (for progressive discovery)
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

    def tool(
        self,
        name: str | None = None,
        description: str | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """
        Decorator to register a function as a tool.

        Args:
            name: Optional tool name (defaults to function name)
            description: Optional description (defaults to docstring)

        Returns:
            Decorator function
        """

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or func.__name__
            tool_desc = description or func.__doc__ or ""

            # Simple schema generation (placeholder for fuller introspection)
            # In a real system, we'd inspect signature to build JSON schema
            # For now, we'll assume the decorated function is well-behaved
            # or rely on manual registration if complex schema needed.
            # But wait, tasks.py usage implies we need schema extraction.
            # Extract schema from function signature using type annotations
            sig = inspect.signature(func)
            # Resolve stringified annotations from `from __future__ import annotations`
            try:
                resolved_hints = get_type_hints(func)
            except Exception:
                resolved_hints = {}
            properties = {}
            required = []

            for param_name, param in sig.parameters.items():
                if param_name == "self" or param_name.startswith("_"):
                    continue

                annotation = resolved_hints.get(param_name, param.annotation)
                param_type = _get_json_schema_type(annotation)
                properties[param_name] = {"type": param_type}

                if param.default == inspect.Parameter.empty:
                    required.append(param_name)

            input_schema = {"type": "object", "properties": properties, "required": required}

            self.register(
                name=tool_name,
                description=tool_desc.strip(),
                input_schema=input_schema,
                func=func,
            )
            return func

        return decorator

    @staticmethod
    def _coerce_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        """Coerce string arguments to their declared schema types.

        MCP arguments may arrive as strings even when the schema declares
        integer, number, boolean, or array types. This method converts
        them based on the tool's input_schema.
        """
        properties = schema.get("properties", {})
        coerced = {}
        for key, value in arguments.items():
            prop = properties.get(key, {})
            declared_type = prop.get("type")

            if not isinstance(value, str) or declared_type is None or declared_type == "string":
                coerced[key] = value
                continue

            try:
                if declared_type == "integer":
                    coerced[key] = int(value)
                elif declared_type == "number":
                    coerced[key] = float(value)
                elif declared_type == "boolean":
                    coerced[key] = value.lower() in ("true", "1", "yes")
                elif declared_type == "array":
                    try:
                        coerced[key] = json.loads(value)
                    except (json.JSONDecodeError, ValueError):
                        coerced[key] = [s.strip() for s in value.split(",") if s.strip()]
                elif declared_type == "object":
                    coerced[key] = json.loads(value)
                else:
                    coerced[key] = value
            except (ValueError, TypeError):
                coerced[key] = value

        return coerced

    async def call(
        self, name: str, arguments: dict[str, Any], context: dict[str, Any] | None = None
    ) -> Any:
        """
        Call a tool by name with the given arguments.

        Args:
            name: Tool name
            arguments: Tool arguments
            context: Optional context dict to inject as ``_context`` (SimpleNamespace)
                for tools that declare that parameter.

        Returns:
            Tool execution result

        Raises:
            ValueError: If tool not found
            Exception: If tool execution fails
        """
        tool = self._tools.get(name)
        if not tool:
            available = ", ".join(self._tools.keys())
            raise ValueError(f"Tool '{name}' not found on '{self.name}'. Available: {available}")

        # Coerce string arguments to declared schema types
        coerced_arguments = self._coerce_arguments(arguments, tool.input_schema)

        # Inspect signature once for both _context injection and kwarg filtering
        sig = inspect.signature(tool.func)
        params = sig.parameters

        # Inject _context for tools that declare it
        if context and "_context" in params:
            coerced_arguments["_context"] = types.SimpleNamespace(**context)

        # Strip unknown kwargs (unless function accepts **kwargs)
        has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if not has_var_keyword:
            accepted = {
                p.name
                for p in params.values()
                if p.kind
                in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            }
            coerced_arguments = {k: v for k, v in coerced_arguments.items() if k in accepted}

        # Call the function (handle both sync and async)
        if inspect.iscoroutinefunction(tool.func):
            return await tool.func(**coerced_arguments)
        return tool.func(**coerced_arguments)

    def list_tools(self) -> list[dict[str, str]]:
        """
        List all tools with lightweight metadata.

        Returns:
            List of {name, brief} dicts for progressive discovery
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

    def get_tool(self, name: str) -> Callable[..., Any] | None:
        """Return tool function by name, or None if not found."""
        tool = self._tools.get(name)
        return tool.func if tool else None

    def get_tool_metadata(self, name: str) -> InternalTool | None:
        """Return full tool metadata (name, description, schema, func) by name."""
        return self._tools.get(name)

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
        logger.info(f"Added internal registry '{registry.name}' with {len(registry)} tools")

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

    def find_tool_server(self, tool_name: str) -> str | None:
        """
        Find which internal server owns a tool.

        Searches all internal registries to find a tool by name.

        Args:
            tool_name: Name of the tool to find

        Returns:
            Server name if found, None otherwise
        """
        for registry in self._registries.values():
            if tool_name in registry._tools:
                return registry.name
        return None

    def __len__(self) -> int:
        """Return number of registries."""
        return len(self._registries)
