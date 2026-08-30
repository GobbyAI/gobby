"""Tool and resource invocation helpers for MCP client manager."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from typing import Any, Protocol

from anyio import BrokenResourceError, ClosedResourceError, EndOfStream
from opentelemetry.trace import Status, StatusCode

from gobby.mcp_proxy.connection_cleanup import describe_exception, discard_connection
from gobby.telemetry.tracing import create_span


class _InvocationManager(Protocol):
    """Manager surface required by MCP invocation helpers."""

    _configs: dict[str, Any]
    _connections: dict[str, Any]
    _lazy_connector: Any
    _tool_schema_cache: dict[str, list[dict[str, Any]]]
    health: dict[str, Any]
    metrics_manager: Any | None
    project_id: str | None

    def get_client_session(self, server_id: str) -> Awaitable[Any]: ...


async def _call_session_tool(
    session: Any,
    tool_name: str,
    arguments: dict[str, Any],
    timeout: float | None,
) -> Any:
    """Call one downstream session, applying the configured timeout."""
    call = session.call_tool(tool_name, arguments)
    if timeout:
        return await asyncio.wait_for(call, timeout=timeout)
    return await call


async def call_tool(
    manager: _InvocationManager,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any] | None,
    timeout: float | None,
    session_id: str | None,
    logger: logging.Logger,
) -> Any:
    """Call a tool on a downstream MCP server with tracing and metrics."""
    start_time = time.perf_counter()
    success = False
    server_config = manager._configs.get(server_id)
    server_label = server_config.name if server_config is not None else server_id
    with create_span(
        "mcp.call_tool",
        attributes={"server_id": server_id, "server_name": server_label, "tool_name": tool_name},
    ) as span:
        try:
            session = await manager.get_client_session(server_id)
            used_connection = manager._connections.get(server_id)
            try:
                result = await _call_session_tool(
                    session,
                    tool_name,
                    arguments or {},
                    timeout,
                )
            except (ClosedResourceError, BrokenResourceError, EndOfStream) as exc:
                error_message = describe_exception(exc)
                logger.warning(
                    "Discarding dead connection for %s after %s failed: %s",
                    server_label,
                    tool_name,
                    error_message,
                )
                if server_id in manager.health:
                    manager.health[server_id].record_failure(error_message)
                await discard_connection(
                    server_id,
                    manager._connections,
                    manager.health,
                    manager._lazy_connector,
                    logger,
                    tool_schema_cache=manager._tool_schema_cache,
                    expected=used_connection,
                )
                session = await manager.get_client_session(server_id)
                result = await _call_session_tool(
                    session,
                    tool_name,
                    arguments or {},
                    timeout,
                )
            if server_id in manager.health:
                manager.health[server_id].record_success()
            success = True
            if span.is_recording():
                span.set_attribute("success", True)
            return result
        except Exception as exc:
            if server_id in manager.health:
                manager.health[server_id].record_failure(str(exc))
            if span.is_recording():
                span.set_attribute("success", False)
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        finally:
            latency_ms = (time.perf_counter() - start_time) * 1000
            if span.is_recording():
                span.set_attribute("latency_ms", latency_ms)

            if manager.metrics_manager:
                server_config = manager._configs.get(server_id)
                metrics_project_id = (
                    server_config.project_id if server_config else manager.project_id
                )
                if metrics_project_id:
                    try:
                        await asyncio.to_thread(
                            manager.metrics_manager.record_call,
                            server_name=server_label,
                            tool_name=tool_name,
                            project_id=metrics_project_id,
                            latency_ms=latency_ms,
                            success=success,
                            session_id=session_id,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to record metrics for %s.%s",
                            server_label,
                            tool_name,
                            exc_info=True,
                        )


async def read_resource(manager: _InvocationManager, server_id: str, uri: str) -> Any:
    """Read a resource from a downstream MCP server."""
    try:
        session = await manager.get_client_session(server_id)
        result = await session.read_resource(uri)
        if server_id in manager.health:
            manager.health[server_id].record_success()
        return result
    except Exception as exc:
        if server_id in manager.health:
            manager.health[server_id].record_failure(str(exc))
        raise
