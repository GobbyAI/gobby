"""Tool and schema execution operations for the tool proxy service."""

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from gobby.hooks.tool_error_tracker import track_proxy_outcome
from gobby.mcp_proxy.models import MCPError, ToolProxyErrorCode
from gobby.mcp_proxy.services.output_repair import maybe_repair_output
from gobby.mcp_proxy.services.server_resolution import (
    caller_project_id,
    resolve_server,
)
from gobby.mcp_proxy.tools.internal import normalize_internal_success_result
from gobby.utils.session_refs import try_resolve_session_field
from gobby.workflows.state_manager import SessionVariableManager

from .schema_guidance import build_invalid_arguments_response
from .tool_proxy_utils import safe_truncate

logger = logging.getLogger("gobby.mcp.server")

PARENT_SESSION_TOOLS = frozenset({"dispatch_batch", "evaluate_spawn", "spawn_agent"})


class ProxyOutcomeClass(StrEnum):
    POLICY_DENIED = "policy_denied"
    INVALID_CALL = "invalid_call"
    FAILED_PRE_DISPATCH = "failed_pre_dispatch"
    EXECUTED = "executed"


@dataclass(frozen=True, slots=True)
class _CallToolOutcome:
    result: Any
    outcome_class: ProxyOutcomeClass
    effective_session_id: str | None
    server_name: str
    tool_name: str
    arguments: dict[str, Any]


def _identity_arguments(arguments: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(arguments, Mapping):
        return deepcopy(dict(arguments))
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        if isinstance(parsed, Mapping):
            return deepcopy(dict(parsed))
    return {}


def _track_proxy_outcome_in_worker(
    variable_manager: SessionVariableManager,
    caller_server_name: str,
    caller_tool_name: str,
    caller_arguments: str | dict[str, Any] | None,
    outcome: _CallToolOutcome,
) -> None:
    track_proxy_outcome(
        variable_manager,
        outcome.effective_session_id,
        (
            caller_server_name,
            caller_tool_name,
            _identity_arguments(caller_arguments),
        ),
        (
            outcome.server_name,
            outcome.tool_name,
            outcome.arguments,
        ),
        outcome.result,
        outcome.outcome_class.value,
    )


def _tracking_variable_manager(service: Any) -> SessionVariableManager | None:
    hook_manager = service._resolve_hook_manager()
    if hook_manager is None:
        return None
    db = getattr(hook_manager, "_database", None)
    if db is None:
        db = getattr(getattr(hook_manager, "_session_manager", None), "db", None)
    return SessionVariableManager(db) if db is not None else None


def _server_is_dispatchable(service: Any, server_name: str) -> bool:
    internal_manager = service._internal_manager
    if internal_manager is not None and internal_manager.is_internal(server_name):
        return internal_manager.get_registry(server_name) is not None
    return bool(service._mcp_manager.has_server(server_name))


def _unknown_server_result(
    service: Any,
    server_name: str,
    tool_name: str,
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    error = f"Server '{server_name}' not found"
    if project_id:
        error += f" in project scope {project_id}"
    suggestion = service._get_server_suggestion(server_name)
    if suggestion:
        error += f". Did you mean '{suggestion}'?"
    return {
        "success": False,
        "error": error,
        "error_code": ToolProxyErrorCode.SERVER_NOT_FOUND.value,
        "server_name": server_name,
        "tool_name": tool_name,
    }


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
    *,
    project_id: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    """List tools for a specific server with progressive discovery format."""
    server_name = service._resolve_server_name(server_name)
    project_id = caller_project_id(service, project_id=project_id, scope=scope)
    if service._is_proxy_namespace(server_name):
        logger.debug("list_tools called with server_name='gobby' — aggregating all internal tools")
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
                brief_tools = await asyncio.to_thread(
                    service._tool_filter.filter_tools, brief_tools, session_id
                )
            return {"success": True, "tools": brief_tools, "tool_count": len(brief_tools)}
        return {"success": True, "tools": [], "tool_count": 0}

    if service._internal_manager and service._internal_manager.is_internal(server_name):
        registry = service._internal_manager.get_registry(server_name)
        if registry:
            tools = registry.list_tools()
            if service._tool_filter and session_id:
                tools = await asyncio.to_thread(
                    service._tool_filter.filter_tools, tools, session_id
                )
            await asyncio.to_thread(
                service.record_listed_server, server_name, session_id=session_id
            )
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

    config = resolve_server(service, server_name, project_id=project_id)
    if config is not None:
        try:
            tools_map = await service._mcp_manager.list_tools(config.id)
        except MCPError as exc:
            return {
                "success": False,
                "tools": [],
                "error": str(exc),
            }
        tools_list = tools_map.get(config.name, tools_map.get(config.id, []))
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
            ext_brief_tools = await asyncio.to_thread(
                service._tool_filter.filter_tools, ext_brief_tools, session_id
            )
        await asyncio.to_thread(service.record_listed_server, server_name, session_id=session_id)
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
    timeout: float | None = None,
    wrapper_originated: bool = False,
    intent: str | None = None,
    project_id: str | None = None,
    scope: str | None = None,
    offload: bool = True,
) -> Any:
    """Execute a tool with optional pre-validation."""
    outcome = await _call_tool_impl(
        service,
        server_name,
        tool_name,
        arguments,
        session_id,
        strip_unknown,
        enforce_workflow,
        timeout,
        wrapper_originated,
        intent,
        project_id=project_id,
        scope=scope,
        offload=offload,
    )
    try:
        sv_mgr = _tracking_variable_manager(service)
        if sv_mgr is not None:
            await asyncio.to_thread(
                _track_proxy_outcome_in_worker,
                sv_mgr,
                server_name,
                tool_name,
                arguments,
                outcome,
            )
    except Exception as exc:
        logger.debug(
            "Failed to track proxy tool outcome for %s/%s: %s",
            server_name,
            tool_name,
            exc,
            exc_info=True,
        )
    return outcome.result


async def _call_tool_impl(
    service: Any,
    server_name: str,
    tool_name: str,
    arguments: str | dict[str, Any] | None = None,
    session_id: str | None = None,
    strip_unknown: bool = False,
    enforce_workflow: bool = True,
    timeout: float | None = None,
    wrapper_originated: bool = False,
    intent: str | None = None,
    project_id: str | None = None,
    scope: str | None = None,
    offload: bool = True,
) -> _CallToolOutcome:
    """Execute one proxy route and return its structural outcome."""
    requested_project_id = project_id
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
        result = await asyncio.to_thread(
            build_invalid_arguments_response,
            service,
            server_name=server_name,
            tool_name=tool_name,
            validation_errors=[error.get("error", "Invalid arguments")],
            input_schema=input_schema,
            session_id=session_id,
            error_message=error.get("error"),
        )
        return _CallToolOutcome(
            result,
            ProxyOutcomeClass.INVALID_CALL,
            None,
            server_name,
            tool_name,
            {},
        )
    arguments = cast("dict[str, Any]", prepared_arguments or {})

    hook_manager = service._resolve_hook_manager()
    session_manager = getattr(service, "session_manager", None)
    if session_manager is None:
        session_manager = getattr(hook_manager, "_session_manager", None) if hook_manager else None
    context_project_id = caller_project_id(
        service,
        project_id=requested_project_id,
        scope=scope,
    )
    await asyncio.to_thread(
        try_resolve_session_field,
        arguments,
        "session_id",
        session_manager=session_manager,
        project_id=context_project_id,
    )

    if service._is_proxy_namespace(server_name):
        resolved = service._resolve_server_for_tool(tool_name)
        if resolved:
            return await _call_tool_impl(
                service,
                resolved,
                tool_name,
                arguments,
                session_id,
                strip_unknown=strip_unknown,
                enforce_workflow=enforce_workflow,
                timeout=timeout,
                wrapper_originated=wrapper_originated,
                intent=intent,
                project_id=requested_project_id,
                scope=scope,
                offload=offload,
            )
        result = {
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
        return _CallToolOutcome(
            result,
            ProxyOutcomeClass.INVALID_CALL,
            None,
            server_name,
            tool_name,
            arguments,
        )

    internal = bool(
        service._internal_manager and service._internal_manager.is_internal(server_name)
    )
    if internal and not _server_is_dispatchable(service, server_name):
        return _CallToolOutcome(
            _unknown_server_result(service, server_name, tool_name, project_id=context_project_id),
            ProxyOutcomeClass.INVALID_CALL,
            None,
            server_name,
            tool_name,
            arguments,
        )

    try:
        effective_session_id = await asyncio.to_thread(
            service._get_effective_session_id, session_id
        )
    except ValueError as exc:
        result = {
            "success": False,
            "error": f"Invalid session reference {session_id!r}: {exc}",
            "error_code": ToolProxyErrorCode.INVALID_ARGUMENTS.value,
            "server_name": server_name,
            "tool_name": tool_name,
        }
        return _CallToolOutcome(
            result,
            ProxyOutcomeClass.INVALID_CALL,
            None,
            server_name,
            tool_name,
            arguments,
        )

    session_project_id: str | None = None
    if effective_session_id and session_manager is not None:
        effective_session = await asyncio.to_thread(session_manager.get, effective_session_id)
        raw_session_project = getattr(effective_session, "project_id", None)
        if isinstance(raw_session_project, str) and raw_session_project:
            session_project_id = raw_session_project
    project_id = caller_project_id(
        service,
        session_project_id=session_project_id,
        project_id=requested_project_id,
        scope=scope,
    )
    dispatch_id: str | None = None
    if not internal:
        config = resolve_server(service, server_name, project_id=project_id)
        if config is None:
            return _CallToolOutcome(
                _unknown_server_result(service, server_name, tool_name, project_id=project_id),
                ProxyOutcomeClass.INVALID_CALL,
                effective_session_id,
                server_name,
                tool_name,
                arguments,
            )
        dispatch_id = config.id
        server_name = config.name

    if enforce_workflow:
        (
            server_name,
            tool_name,
            enforced_arguments,
            workflow_error,
            before_outcome,
        ) = await service._apply_before_tool_enforcement(
            server_name=server_name,
            tool_name=tool_name,
            arguments=arguments,
            session_id=effective_session_id,
        )
        arguments = cast("dict[str, Any]", enforced_arguments)
        if workflow_error is not None:
            if before_outcome is None:
                raise AssertionError("before-tool failure missing outcome class")
            return _CallToolOutcome(
                workflow_error,
                ProxyOutcomeClass(before_outcome),
                effective_session_id,
                server_name,
                tool_name,
                arguments,
            )

    internal = bool(
        service._internal_manager and service._internal_manager.is_internal(server_name)
    )
    if internal:
        dispatch_id = None
        if not _server_is_dispatchable(service, server_name):
            return _CallToolOutcome(
                _unknown_server_result(service, server_name, tool_name, project_id=project_id),
                ProxyOutcomeClass.INVALID_CALL,
                effective_session_id,
                server_name,
                tool_name,
                arguments,
            )
    else:
        config = resolve_server(service, server_name, project_id=project_id)
        if config is None:
            return _CallToolOutcome(
                _unknown_server_result(service, server_name, tool_name, project_id=project_id),
                ProxyOutcomeClass.INVALID_CALL,
                effective_session_id,
                server_name,
                tool_name,
                arguments,
            )
        dispatch_id = config.id
        server_name = config.name

    if service._tool_filter and effective_session_id:
        allowed, reason = await asyncio.to_thread(
            service._tool_filter.is_tool_allowed, tool_name, effective_session_id
        )
        if not allowed:
            result = {
                "success": False,
                "error": reason,
                "error_code": ToolProxyErrorCode.TOOL_BLOCKED.value,
                "server_name": server_name,
                "tool_name": tool_name,
            }
            return _CallToolOutcome(
                result,
                ProxyOutcomeClass.POLICY_DENIED,
                effective_session_id,
                server_name,
                tool_name,
                arguments,
            )

    should_check_schema = service._validate_arguments
    if should_check_schema:
        validation_schema_result: dict[str, Any] | None = None
        try:
            validation_schema_result = await service.get_tool_schema(server_name, tool_name)
        except Exception as schema_error:
            logger.debug("Could not fetch schema for pre-validation: %s", schema_error)
        if validation_schema_result and validation_schema_result.get("success"):
            input_schema = validation_schema_result.get("tool", {}).get("inputSchema", {})
            if input_schema:
                _inject_required_session_id_argument(
                    arguments,
                    input_schema,
                    effective_session_id,
                )
                _inject_required_project_id_argument(arguments, input_schema, project_id)
                if strip_unknown:
                    properties = input_schema.get("properties", {})
                    unknown_keys = [k for k in arguments if k not in properties]
                    for k in unknown_keys:
                        del arguments[k]
                validation_errors = service._check_arguments(arguments, input_schema)
                if validation_errors:
                    error_message = None
                    if strip_unknown:
                        required = input_schema.get("required", [])
                        missing = [r for r in required if r not in arguments]
                        if missing:
                            error_message = f"Missing required parameters: {missing}"
                    result = await asyncio.to_thread(
                        build_invalid_arguments_response,
                        service,
                        server_name=server_name,
                        tool_name=tool_name,
                        validation_errors=validation_errors,
                        input_schema=input_schema,
                        session_id=effective_session_id,
                        error_message=error_message,
                    )
                    return _CallToolOutcome(
                        result,
                        ProxyOutcomeClass.INVALID_CALL,
                        effective_session_id,
                        server_name,
                        tool_name,
                        arguments,
                    )

    result = await _execute_tool_dispatch(
        service=service,
        server_name=server_name,
        tool_name=tool_name,
        arguments=arguments,
        effective_session_id=effective_session_id,
        dispatch_id=dispatch_id,
        project_id=project_id,
        emit_after_workflow=enforce_workflow,
        timeout=timeout,
        wrapper_originated=wrapper_originated,
        intent=intent,
        offload=offload,
    )
    return _CallToolOutcome(
        result,
        ProxyOutcomeClass.EXECUTED,
        effective_session_id,
        server_name,
        tool_name,
        arguments,
    )


async def _execute_tool_dispatch(
    *,
    service: Any,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    effective_session_id: str | None,
    project_id: str | None,
    emit_after_workflow: bool,
    timeout: float | None,
    wrapper_originated: bool,
    intent: str | None,
    offload: bool = True,
    dispatch_id: str | None = None,
) -> Any:
    result = await _execute_tool(
        service=service,
        server_name=server_name,
        tool_name=tool_name,
        arguments=arguments,
        effective_session_id=effective_session_id,
        project_id=project_id,
        timeout=timeout,
        dispatch_id=dispatch_id,
    )
    result = await maybe_repair_output(
        service=service,
        server_name=server_name,
        tool_name=tool_name,
        result=result,
        project_id=project_id,
        dispatch_id=dispatch_id,
    )
    if emit_after_workflow:
        try:
            await service._apply_after_tool_workflow(
                server_name=server_name,
                tool_name=tool_name,
                arguments=arguments,
                session_id=effective_session_id,
                tool_output=result,
            )
        except Exception:
            logger.exception(
                "After-tool workflow failed for %s/%s; preserving tool result",
                server_name,
                tool_name,
            )
    if offload and wrapper_originated and service._result_offloader is not None:
        result = await service._result_offloader.maybe_offload(
            server_name=server_name,
            tool_name=tool_name,
            result=result,
            session_id=effective_session_id,
            intent=intent,
            project_id=project_id,
        )
    return result


async def _record_internal_call_metrics(
    *,
    service: Any,
    server_name: str,
    tool_name: str,
    project_id: str | None,
    session_id: str | None,
    latency_ms: float,
    success: bool,
) -> None:
    metrics_manager = getattr(service._mcp_manager, "metrics_manager", None)
    if metrics_manager is None or not project_id:
        return
    try:
        await asyncio.to_thread(
            metrics_manager.record_call,
            server_name=server_name,
            tool_name=tool_name,
            project_id=project_id,
            latency_ms=latency_ms,
            success=success,
            session_id=session_id,
        )
    except Exception:
        logger.warning(
            "Failed to record metrics for internal tool %s.%s",
            server_name,
            tool_name,
            exc_info=True,
        )


async def _execute_tool(
    *,
    service: Any,
    server_name: str,
    tool_name: str,
    arguments: dict[str, Any],
    effective_session_id: str | None,
    project_id: str | None,
    timeout: float | None,
    dispatch_id: str | None = None,
) -> Any:
    try:
        if service._internal_manager and service._internal_manager.is_internal(server_name):
            start_time = time.perf_counter()
            success = False
            try:
                registry = service._internal_manager.get_registry(server_name)
                if registry:
                    _inject_agent_parent_session_argument(
                        server_name,
                        tool_name,
                        arguments,
                        effective_session_id,
                    )
                    result = await registry.call(tool_name, arguments)
                    normalized_result = normalize_internal_success_result(result)
                    success = True
                    return normalized_result

                error_msg = f"Internal server '{server_name}' not found"
                suggestion = service._get_server_suggestion(server_name)
                if suggestion:
                    error_msg += f". Did you mean '{suggestion}'?"
                raise MCPError(error_msg)
            finally:
                await _record_internal_call_metrics(
                    service=service,
                    server_name=server_name,
                    tool_name=tool_name,
                    project_id=project_id,
                    session_id=effective_session_id,
                    latency_ms=(time.perf_counter() - start_time) * 1000,
                    success=success,
                )

        call_kwargs: dict[str, Any] = {"session_id": effective_session_id}
        if timeout is not None:
            call_kwargs["timeout"] = timeout
        manager_id = dispatch_id or server_name
        result = await service._mcp_manager.call_tool(
            manager_id, tool_name=tool_name, arguments=arguments, **call_kwargs
        )
        return result

    except Exception as e:
        if isinstance(e, TimeoutError):
            error_message = (
                f"Tool call timed out after {timeout:g} seconds"
                if timeout is not None
                else "Tool call timed out"
            )
        else:
            error_message = str(e)
        is_argument_error = service._is_argument_error(error_message)
        log_failure = logger.debug if is_argument_error else logger.warning
        log_failure("Tool call failed: %s/%s: %s", server_name, tool_name, error_message)

        response: dict[str, Any] = {
            "success": False,
            "error": error_message,
            "error_code": service._classify_error(error_message, e),
            "server_name": server_name,
            "tool_name": tool_name,
        }

        if is_argument_error:
            input_schema: dict[str, Any] | None = None
            try:
                schema_result = await service.get_tool_schema(server_name, tool_name)
                if schema_result.get("success"):
                    input_schema = schema_result.get("tool", {}).get("inputSchema", {})
            except Exception as schema_error:
                logger.debug("Could not fetch schema for error enrichment: %s", schema_error)
            response = await asyncio.to_thread(
                build_invalid_arguments_response,
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
                logger.debug("Fallback resolver failed: %s", fallback_error)
                response["fallback_suggestions"] = []
        else:
            response["fallback_suggestions"] = []

        return response


async def get_tool_schema(
    service: Any,
    server_name: str,
    tool_name: str,
    *,
    project_id: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    """Get full schema for a specific tool."""
    server_name = service._resolve_server_name(server_name)
    project_id = caller_project_id(service, project_id=project_id, scope=scope)
    if service._is_proxy_namespace(server_name):
        resolved = service._resolve_server_for_tool(tool_name)
        if resolved:
            return cast(
                "dict[str, Any]",
                await service.get_tool_schema(
                    resolved, tool_name, project_id=project_id, scope=scope
                ),
            )
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

    config = resolve_server(service, server_name, project_id=project_id)
    if config is None:
        error_msg = f"Server '{server_name}' not found in project scope {project_id}"
        suggestion = service._get_server_suggestion(server_name)
        if suggestion:
            error_msg += f". Did you mean '{suggestion}'?"
        return {"success": False, "error": error_msg}

    try:
        result = await service._mcp_manager.get_tool_input_schema(config.id, tool_name)
        return {
            "success": True,
            "tool": {
                "name": tool_name,
                "inputSchema": cast("dict[str, Any]", result),
            },
        }
    except Exception as e:
        raise MCPError(f"Failed to get schema for {tool_name} on {server_name}: {e}") from e


async def call_tool_by_name(
    service: Any,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    session_id: str | None = None,
    *,
    project_id: str | None = None,
    scope: str | None = None,
) -> Any:
    """Call a tool by name, automatically resolving the server."""
    project_id = caller_project_id(service, project_id=project_id, scope=scope)
    server_name = service.find_tool_server(tool_name, project_id=project_id)

    if server_name is None:
        logger.warning("Tool '%s' not found on any server", tool_name)
        return {
            "success": False,
            "error": f"Tool '{tool_name}' not found on any available server",
            "tool_name": tool_name,
        }

    logger.debug("Routing tool '%s' to server '%s'", tool_name, server_name)
    return await service.call_tool(
        server_name,
        tool_name,
        arguments,
        session_id,
        project_id=project_id,
        scope=scope,
    )


__all__ = [
    "call_tool",
    "call_tool_by_name",
    "get_tool_schema",
    "list_tools",
]
