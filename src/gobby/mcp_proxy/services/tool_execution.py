"""Tool and schema execution operations for the tool proxy service."""

import logging
from typing import Any, cast

from gobby.mcp_proxy.models import MCPError, ToolProxyErrorCode
from gobby.mcp_proxy.tools.internal import normalize_internal_success_result

from .tool_proxy_utils import safe_truncate

logger = logging.getLogger("gobby.mcp.server")


def _schema_requires_session_id(input_schema: dict[str, Any]) -> bool:
    required = input_schema.get("required", [])
    return isinstance(required, list) and "session_id" in required


def _missing_target_session_id_error(
    *,
    server_name: str,
    tool_name: str,
    input_schema: dict[str, Any],
) -> dict[str, Any]:
    return {
        "success": False,
        "error": (
            f"Missing required parameter: arguments.session_id for {server_name}:{tool_name}. "
            "The top-level call_tool.session_id is Gobby wrapper context only; target tool "
            "parameters must be supplied inside arguments."
        ),
        "hint": "Pass session_id inside arguments when the target tool schema requires it.",
        "schema": input_schema,
        "server_name": server_name,
        "tool_name": tool_name,
    }


async def list_tools(
    service: Any,
    server_name: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """List tools for a specific server with progressive discovery format."""
    server_name = service._resolve_server_name(server_name)
    if service._is_proxy_namespace(server_name):
        logger.warning(
            "list_tools called with server_name='gobby' — aggregating all internal tools"
        )
        if service._internal_manager:
            brief_tools: list[dict[str, Any]] = []
            for reg in service._internal_manager.get_all_registries():
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
            if service._tool_filter and session_id:
                brief_tools = service._tool_filter.filter_tools(brief_tools, session_id)
            return {"success": True, "tools": brief_tools, "tool_count": len(brief_tools)}
        return {"success": True, "tools": [], "tool_count": 0}

    if service._internal_manager and service._internal_manager.is_internal(server_name):
        registry = service._internal_manager.get_registry(server_name)
        if registry:
            tools = registry.list_tools()
            if service._tool_filter and session_id:
                tools = service._tool_filter.filter_tools(tools, session_id)
            service.record_listed_server(server_name, session_id=session_id)
            return {"success": True, "tools": tools, "tool_count": len(tools)}
        error_msg = f"Internal server '{server_name}' not found"
        suggestion = service._get_server_suggestion(server_name)
        if suggestion:
            error_msg += f". Did you mean '{suggestion}'?"
        return {
            "success": False,
            "tools": [],
            "error": error_msg,
        }

    if service._mcp_manager.has_server(server_name):
        tools_map = await service._mcp_manager.list_tools(server_name)
        tools_list = tools_map.get(server_name, [])
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
        if service._tool_filter and session_id:
            ext_brief_tools = service._tool_filter.filter_tools(ext_brief_tools, session_id)
        service.record_listed_server(server_name, session_id=session_id)
        return {"success": True, "tools": ext_brief_tools, "tool_count": len(ext_brief_tools)}

    error_msg = f"Server '{server_name}' not found"
    suggestion = service._get_server_suggestion(server_name)
    if suggestion:
        error_msg += f". Did you mean '{suggestion}'?"
    return {
        "success": False,
        "tools": [],
        "error": error_msg,
    }


async def call_tool(
    service: Any,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    session_id: str | None = None,
    strip_unknown: bool = False,
    enforce_workflow: bool = True,
) -> Any:
    """Execute a tool with optional pre-validation."""
    server_name = service._resolve_server_name(server_name)
    prepared_arguments, error = service._prepare_arguments(arguments)
    if error is not None:
        return error
    arguments = prepared_arguments or {}

    if service._is_proxy_namespace(server_name):
        resolved = service._resolve_server_for_tool(tool_name)
        if resolved:
            return await service.call_tool(
                resolved,
                tool_name,
                arguments,
                session_id,
                strip_unknown=strip_unknown,
                enforce_workflow=enforce_workflow,
            )
        return {
            "success": False,
            "error": (
                f"Tool '{tool_name}' not found on any server "
                "(server_name='gobby' is not a real server — use list_mcp_servers() "
                "to discover server names)"
            ),
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
        ) = await service._apply_before_tool_enforcement(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
            session_id=session_id,
        )
        if workflow_error is not None:
            return workflow_error

    effective_session_id = service._get_effective_session_id(session_id)

    if service._tool_filter and effective_session_id:
        allowed, reason = service._tool_filter.is_tool_allowed(tool_name, effective_session_id)
        if not allowed:
            return {
                "success": False,
                "error": reason,
                "error_code": ToolProxyErrorCode.TOOL_BLOCKED.value,
                "server_name": server_name,
                "tool_name": tool_name,
            }

    should_check_schema = service._validate_arguments and (
        bool(arguments) or session_id is not None
    )
    if should_check_schema:
        schema_result = await service.get_tool_schema(server_name, tool_name)
        if schema_result.get("success"):
            input_schema = schema_result.get("tool", {}).get("inputSchema", {})
            if input_schema:
                if (
                    session_id is not None
                    and "session_id" not in arguments
                    and _schema_requires_session_id(input_schema)
                ):
                    return _missing_target_session_id_error(
                        server_name=server_name,
                        tool_name=tool_name,
                        input_schema=input_schema,
                    )
                if not arguments:
                    return await _execute_tool(
                        service=service,
                        server_name=server_name,
                        tool_name=tool_name,
                        arguments=arguments,
                        effective_session_id=effective_session_id,
                        enforce_workflow=enforce_workflow,
                    )
                if strip_unknown:
                    properties = input_schema.get("properties", {})
                    unknown_keys = [k for k in arguments if k not in properties]
                    for k in unknown_keys:
                        del arguments[k]
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
                    validation_errors = service._check_arguments(arguments, input_schema)
                    if validation_errors:
                        return {
                            "success": False,
                            "error": f"Invalid arguments: {validation_errors}",
                            "hint": "Review the schema below and retry with correct parameters",
                            "schema": input_schema,
                            "server_name": server_name,
                            "tool_name": tool_name,
                        }

    return await _execute_tool(
        service=service,
        server_name=server_name,
        tool_name=tool_name,
        arguments=arguments,
        effective_session_id=effective_session_id,
        enforce_workflow=enforce_workflow,
    )


async def _execute_tool(
    *,
    service: Any,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    effective_session_id: str | None,
    enforce_workflow: bool,
) -> Any:
    try:
        if service._internal_manager and service._internal_manager.is_internal(server_name):
            registry = service._internal_manager.get_registry(server_name)
            if registry:
                result = await registry.call(tool_name, arguments)
                normalized_result = normalize_internal_success_result(result)
                await service._emit_synthetic_after_tool(
                    effective_session_id=effective_session_id,
                    server_name=server_name,
                    tool_name=tool_name,
                    arguments=arguments,
                    result=normalized_result,
                    enforce_workflow=enforce_workflow,
                    is_failure=False,
                )
                return normalized_result

            error_msg = f"Internal server '{server_name}' not found"
            suggestion = service._get_server_suggestion(server_name)
            if suggestion:
                error_msg += f". Did you mean '{suggestion}'?"
            raise MCPError(error_msg)

        result = await service._mcp_manager.call_tool(
            server_name, tool_name, arguments, session_id=effective_session_id
        )
        await service._emit_synthetic_after_tool(
            effective_session_id=effective_session_id,
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            enforce_workflow=enforce_workflow,
            is_failure=False,
        )
        return result

    except Exception as e:
        error_message = str(e)
        logger.warning(f"Tool call failed: {server_name}/{tool_name}: {error_message}")

        response: dict[str, Any] = {
            "success": False,
            "error": error_message,
            "error_code": service._classify_error(error_message, e),
            "server_name": server_name,
            "tool_name": tool_name,
        }

        if service._is_argument_error(error_message):
            try:
                schema_result = await service.get_tool_schema(server_name, tool_name)
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

        if service._fallback_resolver:
            try:
                project_id = service._mcp_manager.project_id
                if not project_id:
                    from gobby.utils.project_context import get_project_context

                    ctx = get_project_context()
                    project_id = ctx.get("id") if ctx else None
                if project_id:
                    suggestions = await service._fallback_resolver.find_alternatives_for_error(
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

        await service._emit_synthetic_after_tool(
            effective_session_id=effective_session_id,
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
            result=response,
            enforce_workflow=enforce_workflow,
            is_failure=True,
        )
        return response


async def get_tool_schema(
    service: Any,
    server_name: str,
    tool_name: str,
    session_id: str | None = None,
    record_discovery: bool = True,
) -> dict[str, Any]:
    """Get full schema for a specific tool."""
    del session_id, record_discovery
    server_name = service._resolve_server_name(server_name)
    if service._is_proxy_namespace(server_name):
        resolved = service._resolve_server_for_tool(tool_name)
        if resolved:
            return cast("dict[str, Any]", await service.get_tool_schema(resolved, tool_name))
        return {
            "success": False,
            "error": (
                f"Tool '{tool_name}' not found on any server "
                "(server_name='gobby' is not a real server — use list_mcp_servers() "
                "to discover server names)"
            ),
            "error_code": ToolProxyErrorCode.SERVER_NOT_FOUND.value,
        }

    if service._internal_manager and service._internal_manager.is_internal(server_name):
        registry = service._internal_manager.get_registry(server_name)
        if registry:
            schema = registry.get_schema(tool_name)
            if schema:
                return {"success": True, "tool": schema}
            return {
                "success": False,
                "error": f"Tool '{tool_name}' not found on '{server_name}'",
            }

        error_msg = f"Internal server '{server_name}' not found"
        suggestion = service._get_server_suggestion(server_name)
        if suggestion:
            error_msg += f". Did you mean '{suggestion}'?"
        return {"success": False, "error": error_msg}

    if not service._mcp_manager.has_server(server_name):
        error_msg = f"Server '{server_name}' not found"
        suggestion = service._get_server_suggestion(server_name)
        if suggestion:
            error_msg += f". Did you mean '{suggestion}'?"
        return {"success": False, "error": error_msg}

    try:
        result = await service._mcp_manager.get_tool_input_schema(server_name, tool_name)
        return cast("dict[str, Any]", result)
    except Exception as e:
        raise MCPError(f"Failed to get schema for {tool_name} on {server_name}: {e}") from e


async def call_tool_by_name(
    service: Any,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> Any:
    """Call a tool by name, automatically resolving the server."""
    server_name = service.find_tool_server(tool_name)

    if server_name is None:
        logger.warning(f"Tool '{tool_name}' not found on any server")
        return {
            "success": False,
            "error": f"Tool '{tool_name}' not found on any available server",
            "tool_name": tool_name,
        }

    logger.debug(f"Routing tool '{tool_name}' to server '{server_name}'")
    return await service.call_tool(server_name, tool_name, arguments, session_id)


__all__ = [
    "call_tool",
    "call_tool_by_name",
    "get_tool_schema",
    "list_tools",
]
