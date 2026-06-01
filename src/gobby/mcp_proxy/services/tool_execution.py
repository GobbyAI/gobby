"""Tool and schema execution operations for the tool proxy service."""

import logging
from typing import Any, cast

from gobby.mcp_proxy.models import MCPError, ToolProxyErrorCode
from gobby.mcp_proxy.tools.internal import normalize_internal_success_result
from gobby.utils.project_context import get_project_context
from gobby.utils.session_refs import try_resolve_session_field

from .schema_guidance import build_invalid_arguments_response
from .tool_proxy_utils import safe_truncate

logger = logging.getLogger("gobby.mcp.server")

PARENT_SESSION_TOOLS = frozenset({"dispatch_batch", "evaluate_spawn", "spawn_agent"})


def _schema_requires_session_id(input_schema: dict[str, Any]) -> bool:
    required = input_schema.get("required", [])
    return isinstance(required, list) and "session_id" in required


def _schema_requires_project_id(input_schema: dict[str, Any]) -> bool:
    required = input_schema.get("required", [])
    return isinstance(required, list) and "project_id" in required


def _inject_required_session_id_argument(
    arguments: dict[str, Any],
    input_schema: dict[str, Any],
    effective_session_id: str | None,
) -> None:
    """Use wrapper/ambient session context for same-session target calls."""
    if (
        effective_session_id
        and "session_id" not in arguments
        and _schema_requires_session_id(input_schema)
    ):
        arguments["session_id"] = effective_session_id


def _inject_required_project_id_argument(
    arguments: dict[str, Any],
    input_schema: dict[str, Any],
    project_id: str | None,
) -> None:
    """Use resolved wrapper/ambient project context for target tools that require it."""
    if project_id and "project_id" not in arguments and _schema_requires_project_id(input_schema):
        arguments["project_id"] = project_id


def _inject_agent_parent_session_argument(
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    effective_session_id: str | None,
) -> None:
    """Keep child agent spawns in the caller's project when parent_session_id is omitted."""
    if (
        effective_session_id
        and server_name == "gobby-agents"
        and tool_name in PARENT_SESSION_TOOLS
        and not arguments.get("parent_session_id")
    ):
        arguments["parent_session_id"] = effective_session_id


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
        try:
            tools_map = await service._mcp_manager.list_tools(server_name)
        except MCPError as exc:
            return {
                "success": False,
                "tools": [],
                "error": str(exc),
            }
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
    arguments: str | dict[str, Any] | None = None,
    session_id: str | None = None,
    strip_unknown: bool = False,
    enforce_workflow: bool = True,
) -> Any:
    """Execute a tool with optional pre-validation."""
    server_name = service._resolve_server_name(server_name)
    prepared_arguments, error = service._prepare_arguments(arguments)
    if error is not None:
        input_schema: dict[str, Any] | None = None
        try:
            schema_result = await service.get_tool_schema(server_name, tool_name)
            if schema_result.get("success"):
                input_schema = schema_result.get("tool", {}).get("inputSchema", {})
        except Exception as schema_error:
            logger.debug("Could not fetch schema for argument preparation error: %s", schema_error)
        return build_invalid_arguments_response(
            service,
            server_name=server_name,
            tool_name=tool_name,
            validation_errors=[error.get("error", "Invalid arguments")],
            input_schema=input_schema,
            session_id=session_id,
            error_message=error.get("error"),
        )
    arguments = cast("dict[str, Any]", prepared_arguments or {})

    hook_manager = service._resolve_hook_manager()
    session_manager = getattr(hook_manager, "_session_manager", None) if hook_manager else None
    project_ctx = get_project_context()
    project_id = project_ctx.get("id") if project_ctx else None
    project_id_from_context = isinstance(project_id, str)
    if not project_id_from_context:
        project_id = None
    if project_id is None:
        manager_project_id = getattr(getattr(service, "_mcp_manager", None), "project_id", None)
        project_id = manager_project_id if isinstance(manager_project_id, str) else None
    try_resolve_session_field(
        arguments,
        "session_id",
        session_manager=session_manager,
        project_id=project_id,
    )

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
        arguments = cast("dict[str, Any]", arguments)

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
        bool(arguments) or effective_session_id is not None or project_id_from_context
    )
    if should_check_schema:
        schema_result = await service.get_tool_schema(server_name, tool_name)
        if schema_result.get("success"):
            input_schema = schema_result.get("tool", {}).get("inputSchema", {})
            if input_schema:
                _inject_required_session_id_argument(
                    arguments,
                    input_schema,
                    effective_session_id,
                )
                _inject_required_project_id_argument(arguments, input_schema, project_id)
                if not arguments:
                    return await _execute_tool_dispatch(
                        service=service,
                        server_name=server_name,
                        tool_name=tool_name,
                        arguments=arguments,
                        effective_session_id=effective_session_id,
                        emit_after_workflow=enforce_workflow,
                    )
                if strip_unknown:
                    properties = input_schema.get("properties", {})
                    unknown_keys = [k for k in arguments if k not in properties]
                    for k in unknown_keys:
                        del arguments[k]
                    required = input_schema.get("required", [])
                    missing = [r for r in required if r not in arguments]
                    if missing:
                        return build_invalid_arguments_response(
                            service,
                            server_name=server_name,
                            tool_name=tool_name,
                            validation_errors=[
                                f"Missing required parameter '{param}'" for param in missing
                            ],
                            input_schema=input_schema,
                            session_id=effective_session_id,
                            error_message=f"Missing required parameters: {missing}",
                        )
                else:
                    validation_errors = service._check_arguments(arguments, input_schema)
                    if validation_errors:
                        return build_invalid_arguments_response(
                            service,
                            server_name=server_name,
                            tool_name=tool_name,
                            validation_errors=validation_errors,
                            input_schema=input_schema,
                            session_id=effective_session_id,
                        )

    return await _execute_tool_dispatch(
        service=service,
        server_name=server_name,
        tool_name=tool_name,
        arguments=arguments,
        effective_session_id=effective_session_id,
        emit_after_workflow=enforce_workflow,
    )


async def _execute_tool_dispatch(
    *,
    service: Any,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    effective_session_id: str | None,
    emit_after_workflow: bool,
) -> Any:
    result = await _execute_tool(
        service=service,
        server_name=server_name,
        tool_name=tool_name,
        arguments=arguments,
        effective_session_id=effective_session_id,
    )
    if emit_after_workflow:
        await service._apply_after_tool_workflow(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
            session_id=effective_session_id,
            tool_output=result,
        )
    return result


async def _execute_tool(
    *,
    service: Any,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    effective_session_id: str | None,
) -> Any:
    try:
        if service._internal_manager and service._internal_manager.is_internal(server_name):
            registry = service._internal_manager.get_registry(server_name)
            if registry:
                _inject_agent_parent_session_argument(
                    server_name,
                    tool_name,
                    arguments,
                    effective_session_id,
                )
                result = await registry.call(tool_name, arguments)
                return normalize_internal_success_result(result)

            error_msg = f"Internal server '{server_name}' not found"
            suggestion = service._get_server_suggestion(server_name)
            if suggestion:
                error_msg += f". Did you mean '{suggestion}'?"
            raise MCPError(error_msg)

        result = await service._mcp_manager.call_tool(
            server_name, tool_name, arguments, session_id=effective_session_id
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
            input_schema: dict[str, Any] | None = None
            try:
                schema_result = await service.get_tool_schema(server_name, tool_name)
                if schema_result.get("success"):
                    input_schema = schema_result.get("tool", {}).get("inputSchema", {})
            except Exception as schema_error:
                logger.debug(f"Could not fetch schema for error enrichment: {schema_error}")
            response = build_invalid_arguments_response(
                service,
                server_name=server_name,
                tool_name=tool_name,
                validation_errors=[error_message],
                input_schema=input_schema,
                session_id=effective_session_id,
                error_message=error_message,
                hint="Review the schema for the accepted arguments.",
            )

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
